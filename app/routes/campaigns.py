"""THE launch engine — builds campaign → ad group(s) → ad payloads and POSTs
them to TikTok for each target account. Handles duplicated ad groups, cost-cap
ladders, spark identity resolution (§9.2–9.4) and pixel wiring (§9.7).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import error_messages, live_log, models, tiktok_api
from ..database import get_db
from ..settings_store import get_settings
from ..templating import render
from . import launch as launch_mod


def apply_source_to_url(url: str, param: str, source: str) -> str:
    """Append ?param=source to the landing URL (respects existing queries)."""
    if not url or not source:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{param}={source}"

router = APIRouter()


class SparkResolveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Spark identity resolution (§9.2–9.4) — never guess.
# ---------------------------------------------------------------------------

def _identity_lists_item(acct: models.AdAccount, identity: dict, item_id: str) -> bool:
    try:
        data = tiktok_api.list_tt_videos(
            acct.access_token, acct.advertiser_id,
            identity["identity_id"], identity.get("identity_type", "TT_USER"))
        for item in data.get("list", []):
            info = item.get("item_info", item)
            if str(info.get("item_id", "")) == str(item_id):
                return True
    except tiktok_api.TikTokError:
        pass
    return False


def resolve_spark(db: Session, acct: models.AdAccount, spark: models.SparkCode) -> dict:
    """Resolve a SparkCode to (identity_id, identity_type, item_id) for THIS account.

    Chain (§9.2): exact auth-code match → identity that actually LISTS the item
    → the account's shared BC_AUTH_TT identity → refuse. The pasted `CT7Q…`
    code is NOT TikTok's internal `#LQRA…=` auth_code, so code matching alone
    is unreliable — ownership verification is the real test.
    """
    identities = tiktok_api.list_identities(acct.access_token, acct.advertiser_id)

    # 1) exact code match across identities' ad-authorized posts
    for ident in identities:
        try:
            data = tiktok_api.list_tt_videos(
                acct.access_token, acct.advertiser_id,
                ident["identity_id"], ident.get("identity_type", "TT_USER"))
        except tiktok_api.TikTokError:
            continue
        for item in data.get("list", []):
            info = item.get("item_info", item)
            if spark.code and info.get("auth_code") == spark.code:
                return {"identity_id": ident["identity_id"],
                        "identity_type": ident.get("identity_type", "TT_USER"),
                        "item_id": str(info.get("item_id", ""))}

    # 2) known item_id (auto-grabbed sparks) → any identity that LISTS it
    if spark.tiktok_item_id:
        for ident in identities:
            if _identity_lists_item(acct, ident, spark.tiktok_item_id):
                return {"identity_id": ident["identity_id"],
                        "identity_type": ident.get("identity_type", "TT_USER"),
                        "item_id": str(spark.tiktok_item_id)}

    # 3) authorize the pasted code on this advertiser, then VERIFY ownership (§9.3)
    if spark.code:
        try:
            authz = tiktok_api.authorize_tt_video(acct.access_token, acct.advertiser_id, spark.code)
            item_id = str(authz.get("item_id", "") or spark.tiktok_item_id or "")
            auth_identity = {"identity_id": authz.get("identity_id", ""),
                             "identity_type": authz.get("identity_type", "AUTH_CODE")}
            if item_id:
                if auth_identity["identity_id"] and _identity_lists_item(acct, auth_identity, item_id):
                    return {**auth_identity, "item_id": item_id}
                # BC-connected creator: the post belongs to the shared BC_AUTH_TT identity
                for ident in identities:
                    if ident.get("identity_type") == "BC_AUTH_TT" and _identity_lists_item(acct, ident, item_id):
                        return {"identity_id": ident["identity_id"],
                                "identity_type": "BC_AUTH_TT",
                                "item_id": item_id}
        except tiktok_api.TikTokError:
            pass

    # 4) unambiguous fallback: exactly ONE authorized post on the whole account
    all_items = []
    for ident in identities:
        try:
            data = tiktok_api.list_tt_videos(
                acct.access_token, acct.advertiser_id,
                ident["identity_id"], ident.get("identity_type", "TT_USER"))
            for item in data.get("list", []):
                info = item.get("item_info", item)
                all_items.append((ident, str(info.get("item_id", ""))))
        except tiktok_api.TikTokError:
            continue
    if len(all_items) == 1:
        ident, item_id = all_items[0]
        return {"identity_id": ident["identity_id"],
                "identity_type": ident.get("identity_type", "TT_USER"),
                "item_id": item_id}

    raise SparkResolveError(
        f"Could not resolve spark '{spark.name or spark.code[:12]}' on account "
        f"{acct.advertiser_id}: no identity verifiably owns the post. "
        "Check the creator is connected to this account (or the Business Center) "
        "and the post is ad-authorized. Refusing to guess.")


# ---------------------------------------------------------------------------
# Pixel resolution (§9.7)
# ---------------------------------------------------------------------------

def resolve_pixel(db: Session, acct: models.AdAccount, pixel_code: str) -> str:
    """pixel CODE -> numeric pixel_id, cached per advertiser."""
    cached = (db.query(models.PixelCache)
              .filter_by(advertiser_id=acct.advertiser_id, pixel_code=pixel_code).first())
    if cached:
        return cached.pixel_id
    pixels = tiktok_api.list_pixels(acct.access_token, acct.advertiser_id)
    for p in pixels:
        if p.get("pixel_code") == pixel_code or str(p.get("pixel_id")) == pixel_code:
            pid = str(p.get("pixel_id"))
            db.add(models.PixelCache(advertiser_id=acct.advertiser_id, pixel_code=pixel_code,
                                     pixel_id=pid, pixel_name=p.get("pixel_name", "")))
            db.commit()
            return pid
    raise tiktok_api.TikTokError(
        "40002", f"Pixel code '{pixel_code}' not found on advertiser {acct.advertiser_id}. "
                 "Create the pixel there first (or fix the code in the preset).")


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def build_campaign_payload(fields: dict, acct: models.AdAccount) -> dict:
    name = fields["campaign_name_pattern"].replace("{account}", acct.advertiser_name or acct.advertiser_id)
    name = name.replace("{date}", datetime.now(timezone.utc).strftime("%m%d"))
    payload: dict = {
        "campaign_name": name[:512],
        "objective_type": fields["objective_type"],
    }
    mode = fields.get("campaign_budget_mode") or "ABO"
    if mode != "ABO" and fields.get("campaign_budget"):
        # CBO: budget optimization on, budget carried at campaign level
        payload["budget_optimize_on"] = True
        payload["budget_mode"] = mode                 # BUDGET_MODE_DAY | BUDGET_MODE_TOTAL
        payload["budget"] = float(fields["campaign_budget"])
        payload["bid_type"] = fields.get("bid_type", "BID_TYPE_NO_BID")
        payload["optimization_goal"] = fields.get("optimization_goal", "CLICK")
    return payload


def build_adgroup_payload(fields: dict, acct: models.AdAccount, campaign_id: str,
                          index: int, bid_price: float | None, pixel_id: str) -> dict:
    suffix = f" #{index + 1}" if fields["duplicates"] > 1 or fields["cost_cap_ladder"] else ""
    payload: dict = {
        "campaign_id": campaign_id,
        "adgroup_name": f"{fields['template_name']}{suffix}"[:512],
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],           # TikTok-only; avoid Pangle (§5)
        "location_ids": fields["location_ids"],
        "gender": fields["gender"],
        "billing_event": fields["billing_event"],
        "optimization_goal": fields["optimization_goal"],
        "pacing": "PACING_MODE_SMOOTH",
        "schedule_type": fields["schedule_type"],
    }
    if fields.get("age_groups"):
        payload["age_groups"] = fields["age_groups"]
    if fields["schedule_type"] == "SCHEDULE_START_END" and fields.get("schedule_start_time"):
        payload["schedule_start_time"] = fields["schedule_start_time"]
    elif fields["schedule_type"] == "SCHEDULE_FROM_NOW":
        payload["schedule_start_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # budget: ABO = per-ad-group; CBO = campaign carries it (BUDGET_MODE_INFINITE here)
    if (fields.get("campaign_budget_mode") or "ABO") == "ABO":
        payload["budget_mode"] = "BUDGET_MODE_DAY"
        payload["budget"] = float(fields["adgroup_budget"])
    else:
        payload["budget_mode"] = "BUDGET_MODE_INFINITE"

    # bidding: ladder entries get an explicit cost cap
    if bid_price is not None:
        payload["bid_type"] = "BID_TYPE_CUSTOM"
        payload["conversion_bid_price"] = float(bid_price)
    else:
        payload["bid_type"] = fields["bid_type"]

    # destination wiring
    dest = fields["destination_type"]
    if dest == "pixel":
        # ⚠ §5/§9.7: pixel ad groups carry pixel_id + optimization_event and
        # MUST NOT send promotion_website_type (omit = UNSET).
        payload["promotion_type"] = "WEBSITE"
        payload["pixel_id"] = pixel_id
        payload["optimization_event"] = fields["optimization_event"]
    elif dest == "lead_form":
        payload["promotion_type"] = "LEAD_GENERATION"
    else:
        payload["promotion_type"] = "WEBSITE"
    return payload


def build_ad_payload(fields: dict, adgroup_id: str, spark_ref: dict | None,
                     spark: models.SparkCode | None) -> dict:
    creative: dict = {
        "ad_name": f"{fields['template_name']} ad"[:512],
        "ad_format": ("CAROUSEL_ADS" if (spark and spark.media_type == "CAROUSEL") else "SINGLE_VIDEO"),
        "ad_text": fields["ad_text"] or " ",
        "call_to_action": fields["call_to_action"],
    }
    if spark_ref:  # Spark ad: promote the creator's own post
        creative["identity_id"] = spark_ref["identity_id"]
        creative["identity_type"] = spark_ref["identity_type"]
        creative["tiktok_item_id"] = spark_ref["item_id"]
    dest = fields["destination_type"]
    if dest == "instant_page" and fields.get("instant_page_id"):
        creative["page_id"] = fields["instant_page_id"]
    elif dest == "lead_form" and fields.get("lead_form_id"):
        creative["page_id"] = fields["lead_form_id"]
    elif fields.get("landing_page_url"):
        creative["landing_page_url"] = fields["landing_page_url"]
    return {"adgroup_id": adgroup_id, "creatives": [creative]}


# ---------------------------------------------------------------------------
# Launch to ONE account (campaign → ad groups → ads) with error capture
# ---------------------------------------------------------------------------

def launch_to_account(db: Session, acct: models.AdAccount, fields: dict, batch_ref: str) -> models.LaunchLog:
    log = models.LaunchLog(
        batch_ref=batch_ref, advertiser_id=acct.advertiser_id,
        advertiser_name=acct.advertiser_name,
        template_id=fields.get("template_id"), template_name=fields.get("template_name", ""))
    try:
        # spark + pixel resolution BEFORE creating anything (fail early, create nothing)
        spark = None
        spark_ref = None
        if fields.get("spark_code_id"):
            spark = db.get(models.SparkCode, int(fields["spark_code_id"]))
            if spark:
                spark_ref = resolve_spark(db, acct, spark)

        # source wiring: the spark's source rides the landing URL (P&L join key)
        if spark and (spark.source or "").strip():
            settings = get_settings(db)
            log.spark_code_id = spark.id
            log.source = spark.source.strip()
            if fields.get("landing_page_url"):
                fields = dict(fields)
                fields["landing_page_url"] = apply_source_to_url(
                    fields["landing_page_url"], settings["url_param"], log.source)
        pixel_id = fields.get("pixel_id") or ""
        if fields["destination_type"] == "pixel":
            if not fields.get("optimization_event"):
                raise tiktok_api.TikTokError(
                    "40002", "Preset has destination=pixel but no optimization event. "
                             "Edit the preset and pick one (it must already exist on the pixel — §9.7).")
            if not pixel_id:
                pixel_id = resolve_pixel(db, acct, fields["pixel_code"])

        camp = tiktok_api.create_campaign(acct.access_token, acct.advertiser_id,
                                          build_campaign_payload(fields, acct))
        campaign_id = str(camp.get("campaign_id"))
        log.campaign_id = campaign_id

        # duplicated ad groups + cost-cap ladder
        ladder = [float(x) for x in fields.get("cost_cap_ladder") or []]
        n = max(int(fields.get("duplicates") or 1), 1)
        plan: list[float | None] = ladder if ladder else [None] * n
        for i, bid in enumerate(plan):
            ag = tiktok_api.create_adgroup(
                acct.access_token, acct.advertiser_id,
                build_adgroup_payload(fields, acct, campaign_id, i, bid, pixel_id))
            adgroup_id = str(ag.get("adgroup_id"))
            if spark_ref or fields.get("landing_page_url") or fields.get("instant_page_id") \
                    or fields.get("lead_form_id"):
                ad_payload = build_ad_payload(fields, adgroup_id, spark_ref, spark)
                if spark_ref:
                    tiktok_api.create_spark_ad(acct.access_token, acct.advertiser_id, ad_payload)
                else:
                    tiktok_api.create_ad(acct.access_token, acct.advertiser_id, ad_payload)

        if spark:
            spark.use_count = (spark.use_count or 0) + 1
            spark.last_used_at = datetime.now(timezone.utc)
        log.ok = True
        live_log.push("launch", f"Launched '{fields['template_name']}' on {acct.advertiser_name or acct.advertiser_id}")
    except SparkResolveError as e:
        log.ok = False
        log.error_code = "SPARK"
        log.error_message = str(e)
        log.error_technical = str(e)
    except tiktok_api.TikTokError as e:
        info = error_messages.explain(e.code, e.message)
        log.ok = False
        log.error_code = info["code"]
        log.error_message = f"{info['friendly']} {info['action']}".strip()
        log.error_technical = f"code={e.code} message={e.message} request_id={e.request_id}"
        live_log.push("error", f"Launch failed on {acct.advertiser_id}: {info['friendly']}")
    except Exception as e:  # never let one account kill the batch
        log.ok = False
        log.error_code = "APP"
        log.error_message = "Unexpected app error during launch."
        log.error_technical = repr(e)
    db.add(log)
    db.commit()
    return log


def run_batch(db: Session, accounts: list[models.AdAccount], fields: dict) -> str:
    from .. import rules as rules_mod
    batch_ref = error_messages.new_ref()
    for acct in accounts:
        log = launch_to_account(db, acct, fields, batch_ref)
        rules_mod.record_launch_outcome(db, acct, log.ok)
    db.commit()
    return batch_ref


# ---------------------------------------------------------------------------
# Routes: single-account "Create Campaign" + result page
# ---------------------------------------------------------------------------

@router.get("/campaigns/launch")
def launch_form(request: Request, db: Session = Depends(get_db)):
    templates = db.query(models.Template).order_by(models.Template.name).all()
    accounts = (db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
                .order_by(models.AdAccount.advertiser_name).all())
    sparks = db.query(models.SparkCode).filter_by(status="active").order_by(models.SparkCode.name).all()
    return render(request, "campaign_launch.html", {
        "templates": templates, "accounts": accounts, "sparks": sparks,
        "title": "Create Campaign",
    })


@router.post("/campaigns/launch")
def launch_submit(request: Request,
                  template_id: int = Form(...),
                  advertiser_id: str = Form(...),
                  spark_code_id: str = Form(""),
                  db: Session = Depends(get_db)):
    template = db.get(models.Template, template_id)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not template or not acct:
        return RedirectResponse("/campaigns/launch?err=missing", status_code=303)
    overrides = {}
    if spark_code_id:
        overrides["spark_code_id"] = int(spark_code_id)
    fields = launch_mod.synthesize(template, overrides)
    batch_ref = run_batch(db, [acct], fields)
    return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)


@router.get("/campaigns/result/{batch_ref}")
def launch_result(request: Request, batch_ref: str, db: Session = Depends(get_db)):
    logs = (db.query(models.LaunchLog).filter_by(batch_ref=batch_ref)
            .order_by(models.LaunchLog.id).all())
    ok = sum(1 for l in logs if l.ok)
    return render(request, "launch_result.html", {
        "logs": logs, "batch_ref": batch_ref, "ok_count": ok,
        "fail_count": len(logs) - ok, "title": f"Launch result · {batch_ref}",
    })


@router.get("/campaigns/{advertiser_id}/{campaign_id}/edit")
def edit_campaign(request: Request, advertiser_id: str, campaign_id: str,
                  db: Session = Depends(get_db)):
    """Manual budget & cost-cap editor for one campaign."""
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    rec = (db.query(models.CampaignRecord)
           .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
    adgroups, err = [], ""
    if acct and acct.access_token:
        try:
            data = tiktok_api.list_adgroups(acct.access_token, advertiser_id, [campaign_id])
            adgroups = data.get("list", [])
        except tiktok_api.TikTokError as e:
            err = f"Couldn't load ad groups (code {e.code}): {e.message}"
    return render(request, "campaign_edit.html", {
        "title": "Edit campaign", "acct": acct, "rec": rec, "adgroups": adgroups,
        "advertiser_id": advertiser_id, "campaign_id": campaign_id,
        "err": err or request.query_params.get("err", ""),
        "ok": request.query_params.get("ok", ""),
    })


@router.post("/campaigns/{advertiser_id}/{campaign_id}/edit")
def apply_campaign_edit(advertiser_id: str, campaign_id: str,
                        campaign_budget: str = Form(""),
                        adgroup_budget_all: str = Form(""),
                        cost_cap_all: str = Form(""),
                        db: Session = Depends(get_db)):
    """Apply whichever fields were filled: CBO campaign budget, all ad-group
    budgets, and/or all cost caps."""
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not acct or not acct.access_token:
        return RedirectResponse(
            f"/campaigns/{advertiser_id}/{campaign_id}/edit?err=no+token", status_code=303)

    def _f(v):
        try:
            return float(v) if str(v).strip() else None
        except ValueError:
            return None

    new_camp_budget = _f(campaign_budget)
    new_ag_budget = _f(adgroup_budget_all)
    new_cap = _f(cost_cap_all)
    changed, errors = [], []

    if new_camp_budget is not None:
        try:
            tiktok_api.update_campaign_budget(acct.access_token, advertiser_id,
                                              campaign_id, new_camp_budget)
            changed.append(f"campaign budget → ${new_camp_budget:.2f}")
            rec = (db.query(models.CampaignRecord)
                   .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
            if rec:
                rec.budget = new_camp_budget
        except tiktok_api.TikTokError as e:
            errors.append(f"campaign budget: code {e.code} {e.message[:80]}")

    if new_ag_budget is not None or new_cap is not None:
        try:
            data = tiktok_api.list_adgroups(acct.access_token, advertiser_id, [campaign_id])
            for ag in data.get("list", []):
                agid = str(ag.get("adgroup_id", ""))
                try:
                    tiktok_api.update_adgroup(acct.access_token, advertiser_id, agid,
                                              budget=new_ag_budget,
                                              conversion_bid_price=new_cap)
                except tiktok_api.TikTokError as e:
                    errors.append(f"ad group {agid[-6:]}: code {e.code}")
            if new_ag_budget is not None:
                changed.append(f"ad group budgets → ${new_ag_budget:.2f}")
            if new_cap is not None:
                changed.append(f"cost caps → ${new_cap:.2f}")
        except tiktok_api.TikTokError as e:
            errors.append(f"listing ad groups: code {e.code}")

    db.add(models.RuleAction(advertiser_id=advertiser_id, campaign_id=campaign_id,
                             rule="manual edit", action="edit",
                             ok=not errors, detail="; ".join(changed + errors) or "no changes"))
    db.commit()
    q = f"ok={'+'.join(changed).replace(' ', '+')}" if changed else "err=no+changes+applied"
    if errors:
        q += f"&err={'+·+'.join(errors)[:180].replace(' ', '+')}"
    return RedirectResponse(f"/campaigns/{advertiser_id}/{campaign_id}/edit?{q}", status_code=303)


@router.post("/campaigns/{advertiser_id}/{campaign_id}/status")
def campaign_status_update(advertiser_id: str, campaign_id: str,
                           operation_status: str = Form(...),
                           db: Session = Depends(get_db)):
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct:
        try:
            tiktok_api.update_campaign_status(acct.access_token, advertiser_id,
                                              [campaign_id], operation_status)
            rec = (db.query(models.CampaignRecord)
                   .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
            if rec:
                rec.operation_status = operation_status
                db.commit()
        except tiktok_api.TikTokError:
            pass
    return RedirectResponse("/status", status_code=303)
