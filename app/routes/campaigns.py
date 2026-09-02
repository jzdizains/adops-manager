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

from .. import error_messages, live_log, models, queries, tiktok_api
from ..database import get_db
from ..settings_store import get_settings
from ..templating import render
from . import launch as launch_mod


def apply_source_to_url(url: str, param: str, source: str) -> str:
    """Append ?param=source to the landing URL (respects existing queries; the
    value is URL-encoded so odd characters can't corrupt the URL). If the URL
    already carries this param it is replaced, never duplicated."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    if not url or not source:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, source))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


CAMPAIGN_NAME_MACRO = "__CAMPAIGN_NAME__"     # TikTok substitutes the campaign name at click time


def _cta(fields: dict) -> dict:
    """Ad-creative CTA fields: a fixed button, or the Dynamic-CTA portfolio id
    resolved for this account (fields["_cta_portfolio_id"]) when CTA is Auto."""
    from .launch import CTA_AUTO
    if fields.get("call_to_action") == CTA_AUTO:
        return {"call_to_action_id": fields["_cta_portfolio_id"]}
    return {"call_to_action": fields["call_to_action"]}


def resolve_cta_portfolio(db: Session, acct: models.AdAccount, fields: dict) -> str:
    """Dynamic CTA ("Auto"): TikTok's recommended CTA set for this account +
    objective + promotion type, turned into a CTA portfolio once and cached.
    Returns "" (caller falls back to a fixed button) if TikTok refuses."""
    objective = fields.get("objective_type") or "WEB_CONVERSIONS"
    promotion = "LEAD_GENERATION" if (objective == "LEAD_GENERATION" or fields.get("destination_type") == "lead_form") else "WEBSITE"
    key = f"cta_portfolio:{acct.advertiser_id}:{objective}:{promotion}"
    cached = queries.get_setting(db, key, "")
    if cached:
        return cached
    try:
        assets = tiktok_api.recommend_ctas(
            acct.access_token, acct.advertiser_id, objective, promotion,
            landing_page_url=fields.get("landing_page_url") or "",
            ad_texts=[fields["ad_text"]] if fields.get("ad_text") else None,
            optimization_goal=fields.get("optimization_goal") or "")
        pid = tiktok_api.create_cta_portfolio(acct.access_token, acct.advertiser_id, assets)
    except tiktok_api.TikTokError as e:
        # Never let the CTA setup sink a launch: fall back to a fixed button and
        # say so ONCE in the Inbox (deduped while the notice is unread).
        msg = ("Auto CTA isn't active yet — TikTok rejected the Dynamic-CTA setup "
               f"(code {e.code}: {(e.message or '')[:120]}). Launches use “Learn more” until it's fixed.")
        exists = (db.query(models.Alert).filter_by(kind="cta_fallback", acknowledged=False).first())
        if not exists:
            db.add(models.Alert(kind="cta_fallback", ref_id=acct.advertiser_id, level="warn", message=msg))
            db.commit()
        live_log.push("error", f"Auto CTA fell back to Learn more on {acct.advertiser_name or acct.advertiser_id}")
        return ""
    queries.set_setting(db, key, pid)
    return pid

def url_safe_name(name: str) -> str:
    """Only [A-Za-z0-9_-]: TikTok doesn't guarantee URL-encoding of substituted
    macros, so a campaign name with spaces or symbols would corrupt ?source=."""
    import re as _re
    return _re.sub(r"_+", "_", _re.sub(r"[^A-Za-z0-9_-]+", "_", name)).strip("_") or "campaign"


def ensure_source(db: Session, obj, prefix: str) -> str:
    """Every launch MUST carry a source or Glitchy can never attribute it.
    A spark/creative saved without one gets a stable auto source (persisted)."""
    src = (getattr(obj, "source", "") or "").strip()
    if not src:
        src = f"{prefix}{obj.id}"
        obj.source = src
        db.flush()
    return src

router = APIRouter()


class SparkResolveError(Exception):
    def __init__(self, message: str, technical: str = ""):
        super().__init__(message)
        self.technical = technical


# ---------------------------------------------------------------------------
# Spark identity resolution (§9.2–9.4) — never guess.
# ---------------------------------------------------------------------------

def _bc_id_for(acct: models.AdAccount, identity_type: str) -> str:
    """TikTok requires identity_authorized_bc_id on every BC_AUTH_TT call."""
    return (acct.owner_bc_id or "") if identity_type == "BC_AUTH_TT" else ""


def _account_identities(acct: models.AdAccount) -> list[dict]:
    """All identities usable on this account. BC-authorized TikTok accounts
    (BC_AUTH_TT) only list when queried WITH the BC id — merge both queries."""
    identities = tiktok_api.list_identities(acct.access_token, acct.advertiser_id)
    if acct.owner_bc_id:
        try:
            bc_idents = tiktok_api.list_identities(
                acct.access_token, acct.advertiser_id,
                identity_type="BC_AUTH_TT",
                identity_authorized_bc_id=acct.owner_bc_id)
        except tiktok_api.TikTokError:
            bc_idents = []
        seen = {i.get("identity_id") for i in identities}
        for i in bc_idents:
            i.setdefault("identity_type", "BC_AUTH_TT")
            if i.get("identity_id") not in seen:
                identities.append(i)
    return identities


def _identity_lists_item(acct: models.AdAccount, identity: dict, item_id: str) -> bool:
    try:
        data = tiktok_api.list_tt_videos(
            acct.access_token, acct.advertiser_id,
            identity["identity_id"], identity.get("identity_type", "TT_USER"),
            identity_authorized_bc_id=_bc_id_for(acct, identity.get("identity_type", "")))
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
    diag: list[str] = []
    diag.append(f"account BC: {acct.owner_bc_id or 'NONE RECORDED — run a sync (Monitor page)'}")
    try:
        identities = _account_identities(acct)
    except tiktok_api.TikTokError as e:
        raise SparkResolveError(
            f"Could not list identities on account {acct.advertiser_id} "
            f"(TikTok code {e.code}).",
            technical=f"identity/get failed: code={e.code} message={e.message}")
    diag.append("identities: " + (", ".join(
        f"{i.get('identity_type', '?')}·…{str(i.get('identity_id', ''))[-4:]}"
        for i in identities) or "NONE"))

    def _ref(ident: dict, item_id: str) -> dict:
        itype = ident.get("identity_type", "TT_USER")
        ref = {"identity_id": ident["identity_id"], "identity_type": itype,
               "item_id": str(item_id)}
        bc = _bc_id_for(acct, itype)
        if bc:
            ref["identity_authorized_bc_id"] = bc
        return ref

    # 1) exact code match across identities' ad-authorized posts
    for ident in identities:
        itype = ident.get("identity_type", "TT_USER")
        try:
            data = tiktok_api.list_tt_videos(
                acct.access_token, acct.advertiser_id,
                ident["identity_id"], itype,
                identity_authorized_bc_id=_bc_id_for(acct, itype))
        except tiktok_api.TikTokError as e:
            diag.append(f"{itype} post list FAILED: code {e.code} {e.message[:60]}")
            continue
        posts = data.get("list", [])
        if posts:
            diag.append(f"{itype}·…{str(ident.get('identity_id', ''))[-4:]}: {len(posts)} ad-authorized post(s)")
        else:
            diag.append(f"{itype}·…{str(ident.get('identity_id', ''))[-4:]}: 0 posts "
                        f"(response keys: {data.get('_keys', [])})")
        for item in posts:
            info = item.get("item_info", item)
            if spark.code and info.get("auth_code") == spark.code:
                return _ref(ident, info.get("item_id", ""))

    # 2) known item_id (auto-grabbed sparks) → any identity that LISTS it
    if spark.tiktok_item_id:
        for ident in identities:
            if _identity_lists_item(acct, ident, spark.tiktok_item_id):
                return _ref(ident, spark.tiktok_item_id)

    # 3) authorize the pasted code on this advertiser, then VERIFY ownership (§9.3)
    def _authz_item_id(data: dict) -> str:
        """The authorize response's item id arrives in several shapes."""
        for key in ("item_id", "tiktok_item_id"):
            if data.get(key):
                return str(data[key])
        for key in ("item_info", "video_info"):
            node = data.get(key)
            if isinstance(node, dict) and (node.get("item_id") or node.get("tiktok_item_id")):
                return str(node.get("item_id") or node.get("tiktok_item_id"))
        for key in ("item_list", "list", "video_list"):
            node = data.get(key)
            if isinstance(node, list) and node:
                first = node[0].get("item_info", node[0]) if isinstance(node[0], dict) else {}
                if first.get("item_id"):
                    return str(first["item_id"])
        return ""

    if spark.code:
        def _remember(ref: dict) -> dict:
            if not spark.tiktok_item_id:      # remember for future launches
                spark.tiktok_item_id = ref["item_id"]
                db.commit()
            return ref

        # 3a) authorize the code (an "already authorized" error is fine — proceed)
        item_id = str(spark.tiktok_item_id or "")
        try:
            authz = tiktok_api.authorize_tt_video(acct.access_token, acct.advertiser_id, spark.code)
            item_id = _authz_item_id(authz if isinstance(authz, dict) else {}) or item_id
            diag.append(f"authorize code: OK, item_id={item_id or '?'}")
        except tiktok_api.TikTokError as e:
            diag.append(f"authorize code: {e.code} {e.message[:60]} (continuing — may already be authorized)")

        # 3b) translate the code into a post id via /tt_video/info/
        if not item_id:
            try:
                info = tiktok_api.tt_video_info(acct.access_token, acct.advertiser_id,
                                                auth_code=spark.code)
                item_id = _authz_item_id(info if isinstance(info, dict) else {})
                diag.append(f"tt_video/info: item_id={item_id or '?'}")
            except tiktok_api.TikTokError as e:
                diag.append(f"tt_video/info failed: code {e.code} {e.message[:60]}")

        # 3c) DIRECT ownership probe per identity (/identity/video/info/) —
        # succeeds only for an identity that can actually use this post
        if item_id:
            probed = []
            for ident in sorted(identities,
                                key=lambda i: {"AUTH_CODE": 0, "BC_AUTH_TT": 1}.get(
                                    i.get("identity_type", ""), 2)):
                itype = ident.get("identity_type", "TT_USER")
                try:
                    tiktok_api.identity_video_info(
                        acct.access_token, acct.advertiser_id,
                        ident["identity_id"], itype, item_id,
                        identity_authorized_bc_id=_bc_id_for(acct, itype))
                    diag.append(f"probe {itype}: OWNS item {item_id}")
                    return _remember(_ref(ident, item_id))
                except tiktok_api.TikTokError as e:
                    probed.append(f"{itype}:{e.code}")
            diag.append("probes: " + (", ".join(probed) or "none"))
            # legacy list-based verification as a last check
            for ident in identities:
                if _identity_lists_item(acct, ident, item_id):
                    return _remember(_ref(ident, item_id))

        # 3d) re-list posts (they appear only AFTER authorize) and match the code
        relisted: list[tuple[dict, dict]] = []   # (identity, item_info)
        for ident in sorted(identities,
                            key=lambda i: 0 if i.get("identity_type") == "AUTH_CODE" else 1):
            itype = ident.get("identity_type", "TT_USER")
            try:
                data = tiktok_api.list_tt_videos(
                    acct.access_token, acct.advertiser_id,
                    ident["identity_id"], itype,
                    identity_authorized_bc_id=_bc_id_for(acct, itype))
            except tiktok_api.TikTokError:
                continue
            for item in data.get("list", []):
                relisted.append((ident, item.get("item_info", item)))
        diag.append(f"re-list after authorize: {len(relisted)} post(s)")
        for ident, info in relisted:
            if info.get("auth_code") == spark.code or (
                    item_id and str(info.get("item_id", "")) == item_id):
                return _remember(_ref(ident, info.get("item_id", "")))
        if len(relisted) == 1:   # single unambiguous arrival
            ident, info = relisted[0]
            return _remember(_ref(ident, info.get("item_id", "")))
    else:
        diag.append("spark has no pasted code (and no known item_id matched)")

    # 4) unambiguous fallback: exactly ONE authorized post on the whole account
    all_items = []
    for ident in identities:
        try:
            data = tiktok_api.list_tt_videos(
                acct.access_token, acct.advertiser_id,
                ident["identity_id"], ident.get("identity_type", "TT_USER"),
                identity_authorized_bc_id=_bc_id_for(acct, ident.get("identity_type", "")))
            for item in data.get("list", []):
                info = item.get("item_info", item)
                all_items.append((ident, str(info.get("item_id", ""))))
        except tiktok_api.TikTokError:
            continue
    diag.append(f"fallback: {len(all_items)} authorized post(s) total across identities")
    if len(all_items) == 1:
        ident, item_id = all_items[0]
        return _ref(ident, item_id)

    raise SparkResolveError(
        f"Could not resolve spark '{spark.name or spark.code[:12]}' on account "
        f"{acct.advertiser_id}: no identity verifiably owns the post. "
        "Check the creator is connected to this account (or the Business Center) "
        "and the post is ad-authorized. Refusing to guess.",
        technical=" → ".join(diag))


# ---------------------------------------------------------------------------
# Instant page / lead form resolution — per-account assets matched BY NAME
# ---------------------------------------------------------------------------

class AssetResolveError(Exception):
    pass


class ConfigError(Exception):
    """Preset/launch configuration problem — the user must fix the preset.
    Never counts against account health (like ASSET)."""
    pass


def resolve_page_asset(db: Session, acct: models.AdAccount, kind: str, name: str) -> str:
    """Find THIS account's copy of the named page/form. kind: instant_page|lead_form.

    Checks the local cache first; on a miss, live-fetches that one account's
    asset list from TikTok (so a freshly-shared page works without a manual
    sync), then refuses with a clear message if the account has no copy."""
    model = models.InstantPage if kind == "instant_page" else models.LeadForm
    id_attr = "page_id" if kind == "instant_page" else "form_id"

    row = (db.query(model)
           .filter(model.owner_advertiser_id == acct.advertiser_id,
                   model.name == name).first())
    if row:
        return getattr(row, id_attr)

    # live re-check for this one account (asset may have just been shared)
    try:
        if kind == "instant_page":
            data = tiktok_api.list_instant_pages(acct.access_token, acct.advertiser_id)
        else:
            data = tiktok_api.list_lead_forms(acct.access_token, acct.advertiser_id)
        for item in data.get("list", []):
            item_id = str(item.get("page_id", item.get("form_id", "")))
            item_name = item.get("title", item.get("name", "")) or ""
            if not item_id:
                continue
            existing = (db.query(model)
                        .filter(getattr(model, id_attr) == item_id,
                                model.owner_advertiser_id == acct.advertiser_id).first())
            if not existing:
                kwargs = {id_attr: item_id, "owner_advertiser_id": acct.advertiser_id,
                          "name": item_name, "status": str(item.get("status", ""))}
                db.add(model(**kwargs))
            elif item_name:
                existing.name = item_name
        db.commit()
        row = (db.query(model)
               .filter(model.owner_advertiser_id == acct.advertiser_id,
                       model.name == name).first())
        if row:
            return getattr(row, id_attr)
    except tiktok_api.TikTokError:
        pass

    label = "instant page" if kind == "instant_page" else "lead form"
    raise AssetResolveError(
        f"Account {acct.advertiser_name or acct.advertiser_id} has no {label} named "
        f"“{name}”. Share it to this account in TikTok (or clone it on the "
        f"{'Instant Pages' if kind == 'instant_page' else 'Lead Forms'} page), then relaunch.")


# ---------------------------------------------------------------------------
# Pixel resolution (§9.7)
# ---------------------------------------------------------------------------

def resolve_pixel(db: Session, acct: models.AdAccount, pixel_code: str) -> str:
    """pixel CODE -> numeric pixel_id, cached per advertiser. With no code set,
    an account with exactly ONE pixel uses it (unambiguous); several = refuse."""
    pixel_code = (pixel_code or "").strip()
    cached = (db.query(models.PixelCache)
              .filter_by(advertiser_id=acct.advertiser_id, pixel_code=pixel_code).first())
    if cached:
        return cached.pixel_id
    pixels = tiktok_api.list_pixels(acct.access_token, acct.advertiser_id)
    if not pixel_code:
        if len(pixels) == 1:
            pid = str(pixels[0].get("pixel_id"))
            db.add(models.PixelCache(advertiser_id=acct.advertiser_id, pixel_code="",
                                     pixel_id=pid, pixel_name=pixels[0].get("pixel_name", "")))
            db.commit()
            return pid
        raise ConfigError(
            f"No pixel picked in the preset and account {acct.advertiser_id} has "
            f"{len(pixels)} pixels — pick the pixel in the preset (Optimization location).")
    def _numeric_id(p: dict) -> str:
        for key in ("pixel_id", "id"):
            v = str(p.get(key, "") or "")
            if v.isdigit():
                return v
        return str(p.get("pixel_id", "") or "")
    for p in pixels:
        if pixel_code in (str(p.get("pixel_code", "")), str(p.get("pixel_id", "")), str(p.get("id", ""))):
            pid = _numeric_id(p)
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

def _campaign_name(fields: dict, acct: models.AdAccount) -> str:
    """Pattern -> unique name. {account}/{date}/{time} substituted; a time
    suffix is appended automatically when the pattern has no {time}, because
    TikTok rejects duplicate campaign names within an account."""
    now = datetime.now(timezone.utc)
    pattern = fields["campaign_name_pattern"]
    name = (pattern
            .replace("{account}", acct.advertiser_name or acct.advertiser_id)
            .replace("{date}", now.strftime("%m%d")))
    if "{time}" in name:
        name = name.replace("{time}", now.strftime("%H%M%S"))
    else:
        name = f"{name} · {now.strftime('%H%M%S')}"
    if fields.get("_url_safe_names"):
        # source mode = campaign: the name IS the P&L source and rides the URL.
        # Make it URL-safe, and globally unique across accounts (two accounts
        # launched in the same second from one preset would otherwise collide).
        name = url_safe_name(name)
        if "{account}" not in pattern:
            name = f"{name}_a{(acct.advertiser_id or '')[-4:]}"
    return name[:512]


def build_campaign_payload(fields: dict, acct: models.AdAccount) -> dict:
    name = _campaign_name(fields, acct)
    payload: dict = {
        "campaign_name": name[:512],
        "objective_type": fields["objective_type"],
    }
    if fields.get("special_industries"):
        payload["special_industries"] = fields["special_industries"]
    mode = fields.get("campaign_budget_mode") or "ABO"
    if mode != "ABO" and fields.get("campaign_budget"):
        # CBO: budget optimization on, budget carried at campaign level
        payload["budget_optimize_on"] = True
        payload["budget_mode"] = mode                 # BUDGET_MODE_DAY | BUDGET_MODE_TOTAL
        payload["budget"] = float(fields["campaign_budget"])
        payload["bid_type"] = fields.get("bid_type", "BID_TYPE_NO_BID")
        payload["optimization_goal"] = fields.get("optimization_goal", "CLICK")
    else:
        # ABO: TikTok now REQUIRES budget_mode on campaign create even when the
        # budget lives on the ad groups — INFINITE = no campaign-level budget
        payload["budget_mode"] = "BUDGET_MODE_INFINITE"
    return payload


def build_adgroup_payload(fields: dict, acct: models.AdAccount, campaign_id: str,
                          index: int, bid_price: float | None, pixel_id: str) -> dict:
    suffix = f" #{index + 1}" if fields["duplicates"] > 1 or fields["cost_cap_ladder"] else ""
    payload: dict = {
        "campaign_id": campaign_id,
        "adgroup_name": f"{fields['template_name']}{suffix}"[:512],
        "location_ids": fields["location_ids"],
        "gender": fields["gender"],
        "billing_event": fields["billing_event"],
        "optimization_goal": fields["optimization_goal"],
        "pacing": fields.get("pacing") or "PACING_MODE_SMOOTH",
        "schedule_type": fields["schedule_type"],
    }
    # placements: TikTok-only (default, avoids Pangle — §5) or TikTok Automatic
    if fields.get("placement_auto"):
        payload["placement_type"] = "PLACEMENT_TYPE_AUTOMATIC"
    else:
        payload["placement_type"] = "PLACEMENT_TYPE_NORMAL"
        payload["placements"] = ["PLACEMENT_TIKTOK"]
    if fields.get("search_enabled"):
        payload["search_result_enabled"] = True
    # targeting extras — only sent when set (omit = TikTok defaults)
    if fields.get("age_groups"):
        payload["age_groups"] = fields["age_groups"]
    if fields.get("languages"):
        payload["languages"] = fields["languages"]
    if fields.get("spending_power"):
        payload["spending_power"] = fields["spending_power"]
    if fields.get("interest_category_ids"):
        payload["interest_category_ids"] = fields["interest_category_ids"]
    if fields.get("audience_ids"):
        payload["audience_ids"] = fields["audience_ids"]
    if fields.get("excluded_audience_ids"):
        payload["excluded_audience_ids"] = fields["excluded_audience_ids"]
    if fields.get("operating_systems"):
        payload["operating_systems"] = [fields["operating_systems"]]
    if fields.get("network_types"):
        payload["network_types"] = fields["network_types"]
    if fields.get("click_attribution_window"):
        payload["click_attribution_window"] = fields["click_attribution_window"]
    if fields.get("view_attribution_window"):
        payload["view_attribution_window"] = fields["view_attribution_window"]

    if fields["schedule_type"] == "SCHEDULE_START_END" and fields.get("schedule_start_time"):
        payload["schedule_start_time"] = fields["schedule_start_time"]
        if fields.get("schedule_end_time"):
            payload["schedule_end_time"] = fields["schedule_end_time"]
    elif fields["schedule_type"] == "SCHEDULE_FROM_NOW":
        payload["schedule_start_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # budget: ABO = per-ad-group; CBO = campaign carries it (BUDGET_MODE_INFINITE here)
    if (fields.get("campaign_budget_mode") or "ABO") == "ABO":
        payload["budget_mode"] = fields.get("adgroup_budget_mode") or "BUDGET_MODE_DAY"
        payload["budget"] = float(fields["adgroup_budget"])
    else:
        payload["budget_mode"] = "BUDGET_MODE_INFINITE"

    # bidding: ladder entries get an explicit cost cap
    if bid_price is not None:
        payload["bid_type"] = "BID_TYPE_CUSTOM"
        payload["conversion_bid_price"] = float(bid_price)
    else:
        payload["bid_type"] = fields["bid_type"]

    # advanced settings (ad-group level flags — §Advanced)
    if fields.get("comment_disabled"):
        payload["comment_disabled"] = True
    if fields.get("video_download_disabled"):
        payload["video_download_disabled"] = True
    if fields.get("share_disabled"):
        payload["share_disabled"] = True

    # destination wiring
    dest = fields["destination_type"]
    if dest == "pixel" or (dest == "website"
                           and fields.get("optimization_goal") == "CONVERT" and pixel_id):
        # ⚠ §5/§9.7: conversion ad groups carry pixel_id + optimization_event and
        # MUST NOT send promotion_website_type (omit = UNSET).
        if fields.get("objective_type") == "LEAD_GENERATION":
            # web-form lead gen: the ad group is a LEAD_GENERATION promotion
            # aimed at an external website — plain WEBSITE promotion under this
            # objective is what triggers TikTok's vague 40002 "error with the
            # Lead Generation advertising objective" (launch has variant
            # fallbacks in case an account expects a different combination)
            payload["promotion_type"] = "LEAD_GENERATION"
            payload["promotion_target_type"] = "EXTERNAL_WEBSITE"
        else:
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
        **_cta(fields),
    }
    if spark_ref:  # Spark ad: promote the creator's own post
        creative["identity_id"] = spark_ref["identity_id"]
        creative["identity_type"] = spark_ref["identity_type"]
        creative["tiktok_item_id"] = spark_ref["item_id"]
        if spark_ref.get("identity_authorized_bc_id"):
            creative["identity_authorized_bc_id"] = spark_ref["identity_authorized_bc_id"]
    dest = fields["destination_type"]
    if dest == "instant_page" and fields.get("instant_page_id"):
        creative["page_id"] = fields["instant_page_id"]
    elif dest == "lead_form" and fields.get("lead_form_id"):
        creative["page_id"] = fields["lead_form_id"]
    elif fields.get("landing_page_url"):
        creative["landing_page_url"] = fields["landing_page_url"]
    return {"adgroup_id": adgroup_id, "creatives": [creative]}


# ---------------------------------------------------------------------------
# Smart+ payload builders (§Smart+) — separate /smart_plus/* endpoints; TikTok
# automates placements/targeting expansion/creative delivery. One campaign →
# ONE ad group (identity lives on the AD GROUP here) → one ad.
# ---------------------------------------------------------------------------

def build_spc_campaign_payload(fields: dict, acct: models.AdAccount) -> dict:
    name = _campaign_name(fields, acct)
    payload: dict = {
        "campaign_name": name[:512],
        "objective_type": fields["objective_type"],
    }
    if fields.get("special_industries"):
        payload["special_industries"] = fields["special_industries"]
    mode = fields.get("campaign_budget_mode") or "ABO"
    if mode != "ABO" and fields.get("campaign_budget"):
        payload["budget_optimize_on"] = True
        payload["budget_mode"] = mode
        payload["budget"] = float(fields["campaign_budget"])
    else:
        payload["budget_mode"] = "BUDGET_MODE_INFINITE"   # required even for ABO
    return payload


def build_spc_adgroup_payload(fields: dict, campaign_id: str, spark_ref: dict | None,
                              pixel_id: str, bid_price: float | None) -> dict:
    dest = fields["destination_type"]
    convert = dest == "pixel" or fields.get("objective_type") == "WEB_CONVERSIONS"
    payload: dict = {
        "campaign_id": campaign_id,
        "adgroup_name": f"{fields['template_name']} · smart+"[:512],
        "promotion_type": "WEBSITE",
        "optimization_goal": "CONVERT" if convert else "CLICK",
        "billing_event": "OCPM" if convert else "CPC",
        "schedule_type": fields["schedule_type"],
    }
    if convert and pixel_id:
        payload["pixel_id"] = pixel_id
        payload["optimization_event"] = fields["optimization_event"]
    # schedule_start_time is REQUIRED on smart+ ad groups
    if fields["schedule_type"] == "SCHEDULE_START_END" and fields.get("schedule_start_time"):
        payload["schedule_start_time"] = fields["schedule_start_time"]
        if fields.get("schedule_end_time"):
            payload["schedule_end_time"] = fields["schedule_end_time"]
    else:
        payload["schedule_type"] = "SCHEDULE_FROM_NOW"
        payload["schedule_start_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # budget: ABO carries it on the ad group; CBO was set at campaign level
    if (fields.get("campaign_budget_mode") or "ABO") == "ABO":
        payload["budget_mode"] = fields.get("adgroup_budget_mode") or "BUDGET_MODE_DAY"
        payload["budget"] = float(fields["adgroup_budget"])
    # bidding
    if bid_price is not None:
        payload["bid_type"] = "BID_TYPE_CUSTOM"
        payload["conversion_bid_price"] = float(bid_price)
    else:
        payload["bid_type"] = "BID_TYPE_NO_BID"
    # identity (spark creator) — lives on the AD GROUP for smart+
    if spark_ref:
        payload["identity_id"] = spark_ref["identity_id"]
        payload["identity_type"] = spark_ref["identity_type"]
        if spark_ref.get("identity_authorized_bc_id"):
            payload["identity_authorized_bc_id"] = spark_ref["identity_authorized_bc_id"]
    # advanced settings
    if fields.get("comment_disabled"):
        payload["comment_disabled"] = True
    if fields.get("video_download_disabled"):
        payload["video_download_disabled"] = True
    if fields.get("share_disabled"):
        payload["share_disabled"] = True
    if fields.get("click_attribution_window"):
        payload["click_attribution_window"] = fields["click_attribution_window"]
    if fields.get("view_attribution_window"):
        payload["view_attribution_window"] = fields["view_attribution_window"]
    # targeting_spec (required) — TikTok expands beyond these seeds
    spec: dict = {"location_ids": fields["location_ids"]}
    if fields.get("gender") and fields["gender"] != "GENDER_UNLIMITED":
        spec["gender"] = fields["gender"]
    for key in ("age_groups", "languages", "interest_category_ids",
                "audience_ids", "excluded_audience_ids", "network_types"):
        if fields.get(key):
            spec[key] = fields[key]
    if fields.get("spending_power"):
        spec["spending_power"] = fields["spending_power"]
    if fields.get("operating_systems"):
        spec["operating_systems"] = [fields["operating_systems"]]
    payload["targeting_spec"] = spec
    return payload


def build_spc_ad_payload(fields: dict, adgroup_id: str, spark_ref: dict,
                         spark: models.SparkCode | None) -> dict:
    ad_format = "CAROUSEL_ADS" if (spark and spark.media_type == "CAROUSEL") else "SINGLE_VIDEO"
    payload: dict = {
        "adgroup_id": adgroup_id,
        "ad_name": f"{fields['template_name']} smart+ ad"[:512],
        "creative_list": [{"creative_info": {
            "ad_format": ad_format,
            "identity_id": spark_ref["identity_id"],
            "identity_type": spark_ref["identity_type"],
            "tiktok_item_id": spark_ref["item_id"],
        }}],
        "call_to_action_list": ([{"call_to_action": c} for c in __import__("app.routes.launch", fromlist=["CTA_AUTO_SET"]).CTA_AUTO_SET]
                                if fields.get("call_to_action") == "AUTO"
                                else [{"call_to_action": fields["call_to_action"]}]),
    }
    if fields.get("ad_text"):
        payload["ad_text_list"] = [{"ad_text": fields["ad_text"]}]
    if fields.get("landing_page_url"):
        payload["landing_page_url_list"] = [{"landing_page_url": fields["landing_page_url"]}]
    return payload


# ---------------------------------------------------------------------------
# Creative library — per-account upload + custom identity (§Creatives)
# ---------------------------------------------------------------------------

def resolve_account_identity(db: Session, acct: models.AdAccount) -> dict:
    """Pick the TikTok-account identity library ads publish under.

    TikTok no longer supports custom identities — non-spark ads must use a real
    TikTok account identity ("only show as ads" dark posts). Prefer the
    BC-linked identity (BC_AUTH_TT), else the first available one."""
    identities = _account_identities(acct)
    if not identities:
        raise ConfigError(
            f"Account {acct.advertiser_name or acct.advertiser_id} has no TikTok "
            "identity — connect a TikTok account to it (or the Business Center) "
            "before launching library creatives.")
    for ident in identities:
        if ident.get("identity_type") == "BC_AUTH_TT":
            out = {"identity_id": ident["identity_id"], "identity_type": "BC_AUTH_TT"}
            if acct.owner_bc_id:
                out["identity_authorized_bc_id"] = acct.owner_bc_id
            return out
    first = identities[0]
    return {"identity_id": first["identity_id"],
            "identity_type": first.get("identity_type", "TT_USER")}


def _resolve_cover(acct: models.AdAccount, video_id: str, poster: str,
                   creative_id: int) -> str:
    """A cover image_id for a video ad — TikTok requires one ("You must upload an
    image"). (1) suggestcover auto-frames (retried while the video encodes), then
    (2) re-upload the poster URL as a fallback."""
    import time as _time
    for _ in range(4):
        try:
            for c in tiktok_api.suggest_video_cover(acct.access_token,
                                                    acct.advertiser_id, video_id):
                cid = str(c.get("id") or c.get("image_id") or "")
                if cid:
                    return cid
        except tiktok_api.TikTokError:
            pass
        _time.sleep(2)          # video still processing — covers not ready yet
    if poster:
        try:
            img = tiktok_api.upload_image_by_url(acct.access_token, acct.advertiser_id,
                                                 poster, f"cover_c{creative_id}")
            return str(img.get("image_id", ""))
        except tiktok_api.TikTokError:
            pass
    return ""


def _upload_creative_to_account(db: Session, acct: models.AdAccount,
                                creative: models.Creative) -> tuple[str, str]:
    """Upload the creative's video (and its auto cover) into THIS account's asset
    library. The video is cached so retries never re-upload; a MISSING cover is
    re-resolved on retry (the video has since finished processing).
    Returns (video_id, cover_id)."""
    cached = (db.query(models.CreativeUpload)
              .filter_by(creative_id=creative.id,
                         advertiser_id=acct.advertiser_id).first())
    if cached and cached.video_id and cached.cover_image_id:
        return cached.video_id, cached.cover_image_id
    if cached and cached.video_id:      # video uploaded before, cover never resolved
        cover = _resolve_cover(acct, cached.video_id, "", creative.id)
        if cover:
            cached.cover_image_id = cover
            db.commit()
        return cached.video_id, cover

    up = tiktok_api.upload_video_file(acct.access_token, acct.advertiser_id,
                                      creative.file_path,
                                      f"c{creative.id}_{creative.file_name}"[:100])
    video_id = str(up.get("video_id", ""))
    if not video_id:
        raise tiktok_api.TikTokError("APP", "video upload returned no video_id")
    poster = up.get("video_cover_url") or up.get("poster_url") or ""
    cover_image_id = _resolve_cover(acct, video_id, poster, creative.id)
    db.add(models.CreativeUpload(creative_id=creative.id,
                                 advertiser_id=acct.advertiser_id,
                                 video_id=video_id, cover_image_id=cover_image_id))
    db.commit()
    return video_id, cover_image_id


CAROUSEL_OBJECTIVES = ("APP_PROMOTION", "WEB_CONVERSIONS", "TRAFFIC", "LEAD_GENERATION", "REACH")


def carousel_slides(db: Session, carousel: models.Creative) -> list[models.Creative]:
    """The carousel's image creatives, in slide order (first = cover)."""
    import json as _json
    try:
        ids = [int(x) for x in _json.loads(carousel.carousel_images or "[]")]
    except (ValueError, TypeError):
        ids = []
    rows = {c.id: c for c in db.query(models.Creative).filter(models.Creative.id.in_(ids)).all()} if ids else {}
    return [rows[i] for i in ids if i in rows]


def _upload_image_to_account(db: Session, acct: models.AdAccount,
                             image: models.Creative) -> tuple[str, str]:
    """Upload an image creative into THIS account's asset library (cached so a
    retry or a second carousel never re-uploads). Returns (image_id, image_url)."""
    cached = (db.query(models.CreativeUpload)
              .filter_by(creative_id=image.id, advertiser_id=acct.advertiser_id).first())
    if cached and cached.image_id:
        return cached.image_id, cached.image_url or ""
    up = tiktok_api.upload_image_file(acct.access_token, acct.advertiser_id,
                                      image.file_path, f"c{image.id}_{image.file_name}"[:100])
    image_id = str(up.get("image_id", "") or "")
    if not image_id:
        raise tiktok_api.TikTokError("APP", "image upload returned no image_id")
    image_url = str(up.get("image_url") or up.get("url") or "")
    if cached:
        cached.image_id, cached.image_url = image_id, image_url
    else:
        db.add(models.CreativeUpload(creative_id=image.id, advertiser_id=acct.advertiser_id,
                                     image_id=image_id, image_url=image_url))
    db.commit()
    return image_id, image_url


def build_carousel_ad_payload(fields: dict, adgroup_id: str, identity: dict,
                              image_ids: list[str], music_id: str) -> dict:
    """Standard Carousel Ad (doc "Create Carousel Ads"): ad_format CAROUSEL_ADS,
    ordered image_ids (first = cover), ONE music_id, one caption + CTA, identity."""
    creative: dict = {
        "ad_name": f"{fields['template_name']} carousel"[:512],
        "ad_format": "CAROUSEL_ADS",
        "ad_text": fields["ad_text"] or " ",
        **_cta(fields),
        "image_ids": list(image_ids),
        "music_id": music_id,
        "identity_id": identity["identity_id"],
        "identity_type": identity["identity_type"],
        "landing_page_url": fields["landing_page_url"],
    }
    if identity.get("identity_authorized_bc_id"):
        creative["identity_authorized_bc_id"] = identity["identity_authorized_bc_id"]
    return {"adgroup_id": adgroup_id, "creatives": [creative]}


def build_library_ad_payload(fields: dict, adgroup_id: str, identity: dict,
                             video_id: str, cover_image_id: str) -> dict:
    creative: dict = {
        "ad_name": f"{fields['template_name']} ad"[:512],
        "ad_format": "SINGLE_VIDEO",
        "ad_text": fields["ad_text"],
        **_cta(fields),
        "identity_id": identity["identity_id"],
        "identity_type": identity["identity_type"],
        "video_id": video_id,
        "landing_page_url": fields["landing_page_url"],
    }
    if identity.get("identity_authorized_bc_id"):
        creative["identity_authorized_bc_id"] = identity["identity_authorized_bc_id"]
    if cover_image_id:
        creative["image_ids"] = [cover_image_id]
    return {"adgroup_id": adgroup_id, "creatives": [creative]}


def build_smart_creative_ad_payload(fields: dict, adgroup_id: str, identity: dict,
                                    materials: list) -> dict:
    """Smart Creative ad: hand TikTok several video+text materials and let it
    auto-combine and fatigue-refresh them (creative_material_mode=SMART_CREATIVE).
    `materials` is a list of (video_id, cover_image_id, ad_text)."""
    creatives = []
    for i, (video_id, cover_image_id, text) in enumerate(materials):
        c: dict = {
            "ad_name": f"{fields['template_name']} smart {i + 1}"[:512],
            "ad_format": "SINGLE_VIDEO",
            "ad_text": text or " ",
            **_cta(fields),
            "identity_id": identity["identity_id"],
            "identity_type": identity["identity_type"],
            "video_id": video_id,
            "landing_page_url": fields["landing_page_url"],
        }
        if identity.get("identity_authorized_bc_id"):
            c["identity_authorized_bc_id"] = identity["identity_authorized_bc_id"]
        if cover_image_id:
            c["image_ids"] = [cover_image_id]
        creatives.append(c)
    return {"adgroup_id": adgroup_id, "creative_material_mode": "SMART_CREATIVE",
            "creatives": creatives}


def _launch_smart_plus(acct: models.AdAccount, fields: dict, spark_ref: dict | None,
                       spark: models.SparkCode | None, pixel_id: str) -> str:
    """Smart+ creation chain. Returns the new campaign_id (raises TikTokError)."""
    dest = fields["destination_type"]
    if dest not in ("pixel", "website"):
        raise ConfigError("Smart+ presets support Website / Pixel destinations only "
                          "(TikTok's Smart+ web flow). Change the destination or turn Smart+ off.")
    if not spark_ref:
        raise ConfigError("Smart+ launches need a spark creative — pick a spark code "
                          "in the preset or at launch time.")
    camp_payload = build_spc_campaign_payload(fields, acct)
    camp = tiktok_api.smart_plus_campaign_create(
        acct.access_token, acct.advertiser_id, camp_payload)
    campaign_id = str(camp.get("campaign_id"))
    ladder = [float(x) for x in fields.get("cost_cap_ladder") or []]
    bid = ladder[0] if ladder else None      # smart+ = single ad group; first cap wins
    ag = tiktok_api.smart_plus_adgroup_create(
        acct.access_token, acct.advertiser_id,
        build_spc_adgroup_payload(fields, campaign_id, spark_ref, pixel_id, bid))
    adgroup_id = str(ag.get("adgroup_id"))
    tiktok_api.smart_plus_ad_create(
        acct.access_token, acct.advertiser_id,
        build_spc_ad_payload(fields, adgroup_id, spark_ref, spark))
    return campaign_id, camp_payload["campaign_name"]


# ---------------------------------------------------------------------------
# Launch to ONE account (campaign → ad groups → ads) with error capture
# ---------------------------------------------------------------------------

def launch_to_account(db: Session, acct: models.AdAccount, fields: dict, batch_ref: str) -> models.LaunchLog:
    log = models.LaunchLog(
        batch_ref=batch_ref, advertiser_id=acct.advertiser_id,
        advertiser_name=acct.advertiser_name,
        template_id=fields.get("template_id"), template_name=fields.get("template_name", ""))
    creative: models.Creative | None = None      # library creative (reserved below)
    carousel: models.Creative | None = None      # carousel creative (reserved below)
    carousel_image_ids: list[str] = []            # per-account uploaded slide ids
    pool_text: models.AdText | None = None
    creative_committed = False                    # True once an ad actually exists
    new_campaign_id = ""                          # campaign THIS launch created (for cleanup)
    ad_created = False                            # True once ANY ad exists
    sc_creatives: list = []                       # Smart Creative: all reserved videos
    sc_texts: list = []                           # Smart Creative: all reserved pool texts
    sc_materials: list = []                       # Smart Creative: (video_id, cover, text)
    try:
        # -- config validation FIRST (free, local — before any API calls) ------
        use_library = fields.get("creative_source") == "library"
        use_carousel = fields.get("creative_source") == "carousel"
        if use_carousel:
            # TikTok "Create Carousel Ads" rules for Standard Carousel Ads
            if fields["objective_type"] not in CAROUSEL_OBJECTIVES:
                raise ConfigError("Carousel Ads need one of these objectives: Leads, Website "
                                  "engagements (conversions), Click (traffic), Reach or App "
                                  f"promotion — this preset uses {fields['objective_type']}.")
            if fields.get("smart_plus"):
                raise ConfigError("Carousel Ads can't run on Smart+ campaigns — turn Smart+ off.")
            if fields.get("smart_creative"):
                raise ConfigError("Carousel Ads can't use Smart Creative (TikTok requires ACO off) — "
                                  "turn Smart Creative off.")
            if fields["destination_type"] not in ("website", "pixel") or not fields.get("landing_page_url"):
                raise ConfigError("Carousel presets need a Website destination with a landing page URL.")
            if not (fields.get("ad_text") or "").strip() and fields.get("ad_text_mode") != "pool":
                raise ConfigError("Carousel Ads need a caption — add ad text to the preset.")
        if fields.get("smart_creative") and not use_library:
            raise ConfigError("Smart Creative pulls several videos from the Creative "
                              "library — set the creative source to “Library” to use it.")
        if fields.get("smart_creative") and fields.get("smart_plus"):
            raise ConfigError("Smart Creative and Smart+ can't be combined — Smart+ has "
                              "its own creative automation. Turn one off.")
        if fields.get("smart_plus"):
            if use_library:
                raise ConfigError("Library creatives aren't supported on Smart+ presets yet "
                                  "— use a spark code for Smart+.")
            if fields["destination_type"] not in ("pixel", "website"):
                raise ConfigError("Smart+ presets support Website / Pixel destinations only "
                                  "(TikTok's Smart+ web flow). Change the destination or turn Smart+ off.")
            if not fields.get("spark_code_id"):
                raise ConfigError("Smart+ launches need a spark creative — pick a spark code "
                                  "in the preset or at launch time.")
        if use_library:
            if fields["destination_type"] not in ("website", "pixel"):
                raise ConfigError("Library creatives support Website / Pixel destinations only.")
            if not fields.get("landing_page_url"):
                raise ConfigError("Library-creative presets need a landing page URL "
                                  "(the video ad's destination).")
            # TikTok now REQUIRES ad text on ads
            if fields.get("ad_text_mode") != "pool" and not (fields.get("ad_text") or "").strip():
                raise ConfigError("TikTok requires ad text on every ad — enter it in the "
                                  "preset, or switch to pulling from the Ad Texts list.")
        elif fields.get("ad_text_mode") == "pool":
            raise ConfigError("The Ad Texts pool works with the Creative library only — "
                              "spark ads always show the post's own caption.")
        needs_pixel = (fields["destination_type"] == "pixel"
                       or (fields["destination_type"] == "website"
                           and fields.get("optimization_goal") == "CONVERT"))
        if needs_pixel and not fields.get("optimization_event"):
            raise ConfigError("Conversion campaigns need a pixel + optimization event — "
                              "pick both in the preset (Optimization location section). "
                              "The event must already exist on the pixel — §9.7.")
        needs_end = (fields.get("adgroup_budget_mode") == "BUDGET_MODE_TOTAL"
                     or (fields.get("campaign_budget_mode") == "BUDGET_MODE_TOTAL"))
        if needs_end and not (fields.get("schedule_type") == "SCHEDULE_START_END"
                              and fields.get("schedule_end_time")):
            raise ConfigError("A TOTAL budget needs an end date — in the preset set "
                              "Schedule to start/end with an end time, or switch the "
                              "budget type to Daily.")
        strategy = fields.get("bid_strategy") or ""
        if strategy == "cost_cap" and not (fields.get("cost_cap_ladder") or []):
            raise ConfigError("Preset bid strategy is Cost cap but no cap value is set. "
                              "Edit the preset and add cap value(s), or switch to Maximum delivery.")
        if strategy == "max_delivery" and fields.get("cost_cap_ladder"):
            fields = dict(fields)
            fields["cost_cap_ladder"] = []      # explicit max delivery ignores caps

        # spark + pixel resolution BEFORE creating anything (fail early, create nothing)
        spark = None
        spark_ref = None
        if not use_library and fields.get("spark_code_id"):
            spark = db.get(models.SparkCode, int(fields["spark_code_id"]))
            if spark:
                spark_ref = resolve_spark(db, acct, spark)

        # ---- source wiring (P&L join key — must reach Glitchy on every launch) ---
        settings = get_settings(db)
        source_mode = settings.get("source_mode", "campaign")
        if source_mode == "campaign":
            # ?source=__CAMPAIGN_NAME__ — TikTok fills in the campaign name at click
            # time, so the source is unique per campaign and needs no per-asset setup.
            # The name is made URL-safe (see _campaign_name) and log.source is set to
            # the literal name once the campaign exists, so the join stays exact.
            fields = dict(fields)
            fields["_url_safe_names"] = True
            if fields.get("landing_page_url"):
                fields["landing_page_url"] = apply_source_to_url(
                    fields["landing_page_url"], settings["url_param"], CAMPAIGN_NAME_MACRO)
                log.landing_url = fields["landing_page_url"]
        if spark:
            log.spark_code_id = spark.id
        if spark and source_mode == "static":
            # legacy: the spark's own source rides the URL. A spark without one
            # gets a stable auto source — a sourceless launch is invisible to Glitchy.
            log.source = ensure_source(db, spark, "sp")
            if fields.get("landing_page_url"):
                fields = dict(fields)
                fields["landing_page_url"] = apply_source_to_url(
                    fields["landing_page_url"], settings["url_param"], log.source)
                log.landing_url = fields["landing_page_url"]

        # Dynamic CTA ("Auto"): resolve/create this account's CTA portfolio first —
        # a failure here surfaces cleanly before any campaign exists
        if fields.get("call_to_action") == "AUTO" and not fields.get("smart_plus"):
            fields = dict(fields)
            pid = resolve_cta_portfolio(db, acct, fields)
            if pid:
                fields["_cta_portfolio_id"] = pid
            else:
                fields["call_to_action"] = "LEARN_MORE"     # graceful fallback (see resolve_cta_portfolio)

        # carousel: reserve the next unused carousel (or the one picked), upload
        # every slide into THIS account, resolve the identity — before creating anything
        creative_video_id = creative_cover_id = ""
        creative_identity: dict = {}
        if use_carousel:
            reuse = bool(fields.get("allow_creative_reuse"))
            if fields.get("creative_id"):
                cid = int(fields["creative_id"])
                carousel = db.get(models.Creative, cid) if cid > 0 else None
                if not carousel or carousel.kind != "carousel":
                    raise ConfigError("The picked creative isn't a carousel.")
                if carousel.status != "available" and not (reuse and carousel.status == "used"):
                    raise ConfigError(f"Carousel “{carousel.name}” has already launched.")
            else:
                carousel = (db.query(models.Creative).filter_by(status="available", kind="carousel")
                            .order_by(models.Creative.id).first())
                if not carousel:
                    raise ConfigError("No available carousels — build one on the Creatives page (Carousels tab).")
            slides = carousel_slides(db, carousel)
            if len(slides) < 2:
                raise ConfigError(f"Carousel “{carousel.name}” has fewer than 2 slides.")
            if not (carousel.music_id or "").strip():
                raise ConfigError(f"Carousel “{carousel.name}” has no soundtrack — TikTok requires one.")
            if carousel.status == "available":
                carousel.status = "used"
                carousel.used_advertiser_id = acct.advertiser_id
                carousel.used_at = datetime.now(timezone.utc)
                db.flush()
            if fields.get("ad_text_mode") == "pool":
                pool_text = (db.query(models.AdText).filter_by(status="available")
                             .order_by(models.AdText.id).first())
                if not pool_text:
                    raise ConfigError("The ad-text pool is empty — add texts or switch to a fixed text.")
                pool_text.status = "used"
                pool_text.used_advertiser_id = acct.advertiser_id
                pool_text.used_at = datetime.now(timezone.utc)
                db.flush()
                fields = dict(fields); fields["ad_text"] = pool_text.text
            carousel_image_ids = [_upload_image_to_account(db, acct, img)[0] for img in slides]
            creative_identity = resolve_account_identity(db, acct)
        if use_library:
            settings = get_settings(db)
            reuse = bool(fields.get("allow_creative_reuse"))
            if fields.get("creative_id"):
                # launcher picked a SPECIFIC creative (single pick, or a Super
                # Launcher group where one creative covers several accounts)
                cid = int(fields["creative_id"])
                creative = db.get(models.Creative, cid) if cid > 0 else None
                if not creative:
                    raise ConfigError(
                        "No creative available for this account — the Super Launcher "
                        "ran out of creatives for the number of accounts requested. "
                        "Add more (or more variations) on the Creatives page."
                        if cid < 0 else
                        "The selected creative no longer exists in the library.")
                if creative.status not in ("available", "used"):
                    raise ConfigError(f"Creative “{creative.name}” isn't ready "
                                      f"(status: {creative.status}).")
                if creative.status == "used" and not reuse:
                    raise ConfigError(f"Creative “{creative.name}” has already been used "
                                      "(each creative launches once) — pick another, or "
                                      "leave the launcher on automatic.")
            else:
                creative = (db.query(models.Creative).filter_by(status="available", kind="video")
                            .order_by(models.Creative.id).first())
            if not creative:
                raise ConfigError("No available creatives left in the library — upload "
                                  "more on the Creatives page (each creative is used once).")
            # reserve immediately so a concurrent launch can't take the same one
            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            creative.status = "used"
            if not creative.used_advertiser_id:
                creative.used_advertiser_id = acct.advertiser_id
            if not creative.used_at:
                creative.used_at = now_naive
            # unique-ad-text pool: reserve the next text too
            if fields.get("ad_text_mode") == "pool":
                pool_text = (db.query(models.AdText)
                             .filter_by(status="available")
                             .order_by(models.AdText.id).first())
                if not pool_text:
                    raise ConfigError("No available ad texts left in the pool — "
                                      "add more on the Ad Texts page.")
                pool_text.status = "used"
                pool_text.used_advertiser_id = acct.advertiser_id
                pool_text.used_at = now_naive
                fields = dict(fields)
                fields["ad_text"] = pool_text.text
            db.commit()
            # ads publish under the account's own TikTok identity (dark post)
            creative_identity = resolve_account_identity(db, acct)
            creative_video_id, creative_cover_id = _upload_creative_to_account(db, acct, creative)
            if not creative_cover_id:
                # TikTok rejects a video ad with no cover ("You must upload an
                # image"). If we still couldn't get one, the video likely hadn't
                # finished processing — a retry (the video is cached now) usually
                # resolves it on the next attempt.
                raise ConfigError(
                    f"Couldn't get a cover image for “{creative.name}” yet — TikTok "
                    "hadn't finished processing the video. Hit Retry in a moment "
                    "(the upload is cached, so it's instant).")
            if source_mode == "static":
                log.source = ensure_source(db, creative, "c")
                if fields.get("landing_page_url"):
                    fields = dict(fields)
                    fields["landing_page_url"] = apply_source_to_url(
                        fields["landing_page_url"], settings["url_param"], log.source)
                    log.landing_url = fields["landing_page_url"]

            # SMART CREATIVE: gather several more videos + texts and let TikTok
            # auto-combine/refresh them. Material #1 is the creative reserved above.
            if fields.get("smart_creative"):
                n_vids = max(int(fields.get("smart_creative_videos") or 5), 1)
                n_txt = max(int(fields.get("smart_creative_texts") or 5), 1)
                sc_creatives = [creative]
                while len(sc_creatives) < n_vids:
                    nxt = (db.query(models.Creative).filter_by(status="available", kind="video")
                           .order_by(models.Creative.id).first())
                    if not nxt:
                        break                       # use however many we have
                    nxt.status = "used"
                    nxt.used_advertiser_id = acct.advertiser_id
                    nxt.used_at = now_naive
                    sc_creatives.append(nxt)
                    db.flush()          # so the next query doesn't re-pick it
                # texts: the pool text (if any) + more from the pool, else the fixed text
                texts: list[str] = []
                if pool_text is not None:
                    sc_texts = [pool_text]
                    texts.append(pool_text.text)
                while len(texts) < n_txt:
                    trow = (db.query(models.AdText).filter_by(status="available")
                            .order_by(models.AdText.id).first())
                    if not trow:
                        break
                    trow.status = "used"
                    trow.used_advertiser_id = acct.advertiser_id
                    trow.used_at = now_naive
                    sc_texts.append(trow)
                    texts.append(trow.text)
                    db.flush()          # so the next query doesn't re-pick it
                if not texts:
                    texts = [fields.get("ad_text") or " "]
                db.commit()
                if len(sc_creatives) < 2:
                    raise ConfigError(
                        "Smart Creative needs at least 2 videos — add more to the "
                        "Creative library (or lower “videos per ad”).")
                # upload every video (material #1 is already uploaded) → materials
                for i, c in enumerate(sc_creatives):
                    if i == 0:
                        vid, cov = creative_video_id, creative_cover_id
                    else:
                        vid, cov = _upload_creative_to_account(db, acct, c)
                        if not cov:
                            raise ConfigError(
                                f"Couldn't get a cover for “{c.name}” yet — TikTok hadn't "
                                "finished processing it. Hit Retry in a moment.")
                    sc_materials.append((vid, cov, texts[i % len(texts)]))
        # per-account page/form resolution (matched by name — fail before creating)
        dest = fields["destination_type"]
        if dest == "instant_page":
            name = fields.get("instant_page_name") or ""
            if not name and fields.get("instant_page_id"):   # legacy exact-id presets
                legacy = (db.query(models.InstantPage)
                          .filter_by(page_id=fields["instant_page_id"]).first())
                name = legacy.name if legacy else ""
            if not name:
                raise ConfigError("Preset destination is Instant Page but no page is selected.")
            fields = dict(fields)
            fields["instant_page_id"] = resolve_page_asset(db, acct, "instant_page", name)
        elif dest == "lead_form":
            name = fields.get("lead_form_name") or ""
            if not name and fields.get("lead_form_id"):
                legacy = (db.query(models.LeadForm)
                          .filter_by(form_id=fields["lead_form_id"]).first())
                name = legacy.name if legacy else ""
            if not name:
                raise ConfigError("Preset destination is Lead Form but no form is selected.")
            fields = dict(fields)
            fields["lead_form_id"] = resolve_page_asset(db, acct, "lead_form", name)

        pixel_id = str(fields.get("pixel_id") or "").strip()
        if pixel_id and not pixel_id.isdigit():
            # the preset stored the pixel CODE (alphanumeric) — TikTok's ad group
            # API wants the NUMERIC pixel id; resolve it per account (cached)
            fields = dict(fields)
            fields["pixel_code"] = pixel_id
            pixel_id = ""
        if needs_pixel and not pixel_id:
            pixel_id = resolve_pixel(db, acct, fields["pixel_code"])
            if not str(pixel_id).isdigit():
                raise ConfigError(
                    f"Could not obtain a numeric pixel id (got '{pixel_id}') — re-sync "
                    "the Pixels page and re-pick the pixel in the preset.")

        created_name = ""
        if fields.get("smart_plus"):
            log.campaign_id, created_name = _launch_smart_plus(acct, fields, spark_ref, spark, pixel_id)
            if source_mode == "campaign":
                log.source = created_name
        else:
            camp_payload = build_campaign_payload(fields, acct)
            camp = tiktok_api.create_campaign(acct.access_token, acct.advertiser_id,
                                              camp_payload)
            campaign_id = str(camp.get("campaign_id"))
            log.campaign_id = campaign_id
            new_campaign_id = campaign_id     # remember for orphan cleanup on failure
            created_name = camp_payload["campaign_name"]
            if source_mode == "campaign":
                log.source = created_name     # exactly what TikTok will put in ?source=

            # duplicated ad groups + cost-cap ladder.
            # duplicates multiplies EVERY bid: one cap + duplicates=10 → 10 ad
            # groups at that cap; caps [5,6] + duplicates=3 → 6 ad groups.
            ladder = [float(x) for x in fields.get("cost_cap_ladder") or []]
            n = max(int(fields.get("duplicates") or 1), 1)
            if ladder:
                plan: list[float | None] = [bid for bid in ladder for _ in range(n)]
            else:
                plan = [None] * n
            for i, bid in enumerate(plan):
                base_payload = build_adgroup_payload(fields, acct, campaign_id, i, bid, pixel_id)
                # lead-gen web accounts differ in which promotion combination they
                # accept — try the documented one first, then graceful variants
                variants: list[dict] = [base_payload]
                if base_payload.get("promotion_type") == "LEAD_GENERATION":
                    no_target = {k: v for k, v in base_payload.items()
                                 if k != "promotion_target_type"}
                    variants.append(no_target)
                    variants.append({**no_target, "promotion_type": "WEBSITE"})

                ag = None
                last_err: tiktok_api.TikTokError | None = None
                for v_i, ag_payload in enumerate(variants):
                    try:
                        try:
                            ag = tiktok_api.create_adgroup(
                                acct.access_token, acct.advertiser_id, ag_payload)
                        except tiktok_api.TikTokError as e:
                            # some accounts now REQUIRE an end time even for daily
                            # budgets — retry once with an explicit 1-year window
                            if "end_time" in (e.message or "") and "schedule_end_time" not in ag_payload:
                                from datetime import timedelta
                                start = ag_payload.get("schedule_start_time") or \
                                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                                end = (datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
                                       + timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
                                ag_payload = {**ag_payload, "schedule_type": "SCHEDULE_START_END",
                                              "schedule_start_time": start,
                                              "schedule_end_time": end}
                                ag = tiktok_api.create_adgroup(
                                    acct.access_token, acct.advertiser_id, ag_payload)
                            else:
                                raise
                        break   # created — stop trying variants
                    except tiktok_api.TikTokError as e:
                        last_err = e
                        msg = (e.message or "").lower()
                        # only walk to the next variant on objective/promotion
                        # complaints; anything else is a real error — surface it
                        if (v_i < len(variants) - 1
                                and ("objective" in msg or "promotion" in msg)):
                            continue
                        raise
                if ag is None:   # defensive — loop always breaks or raises
                    raise last_err or tiktok_api.TikTokError("APP", "ad group not created")
                adgroup_id = str(ag.get("adgroup_id"))

                # SMART CREATIVE: one auto-combining ad per ad group from all the
                # reserved materials (ignores ads-per-group — TikTok rotates internally)
                if fields.get("smart_creative") and sc_materials:
                    ad_payload = build_smart_creative_ad_payload(
                        fields, adgroup_id, creative_identity, sc_materials)
                    tiktok_api.create_ad(acct.access_token, acct.advertiser_id, ad_payload)
                    creative_committed = True
                    ad_created = True
                    for c in sc_creatives:
                        if not c.used_campaign_id:
                            c.used_campaign_id = campaign_id
                    for trow in sc_texts:
                        if not trow.used_campaign_id:
                            trow.used_campaign_id = campaign_id
                    continue     # next ad group

                n_ads = max(int(fields.get("ads_per_group") or 1), 1)
                for ad_i in range(n_ads):
                    def _uniq(p: dict) -> dict:
                        # duplicate ads in one group need distinct names — the name
                        # sits top-level (spark ads) or inside creatives[] (library)
                        if n_ads <= 1:
                            return p
                        suffix = f" g{i + 1}-{ad_i + 1}"   # unique across the campaign
                        p = {**p}
                        if p.get("ad_name"):
                            p["ad_name"] = f"{p['ad_name']}{suffix}"[:512]
                        if isinstance(p.get("creatives"), list):
                            p["creatives"] = [
                                {**c, "ad_name": f"{c.get('ad_name', 'ad')}{suffix}"[:512]}
                                for c in p["creatives"]]
                        return p
                    if carousel is not None:
                        ad_payload = _uniq(build_carousel_ad_payload(
                            fields, adgroup_id, creative_identity,
                            carousel_image_ids, carousel.music_id))
                        tiktok_api.create_ad(acct.access_token, acct.advertiser_id, ad_payload)
                        creative_committed = True
                        ad_created = True
                        if not carousel.used_campaign_id:
                            carousel.used_campaign_id = campaign_id
                        if pool_text is not None and not pool_text.used_campaign_id:
                            pool_text.used_campaign_id = campaign_id
                    elif creative is not None:
                        ad_payload = _uniq(build_library_ad_payload(
                            fields, adgroup_id, creative_identity,
                            creative_video_id, creative_cover_id))
                        tiktok_api.create_ad(acct.access_token, acct.advertiser_id, ad_payload)
                        creative_committed = True
                        ad_created = True
                        if not creative.used_campaign_id:
                            creative.used_campaign_id = campaign_id
                        if pool_text is not None and not pool_text.used_campaign_id:
                            pool_text.used_campaign_id = campaign_id
                    elif spark_ref or fields.get("landing_page_url") or fields.get("instant_page_id") \
                            or fields.get("lead_form_id"):
                        ad_payload = _uniq(build_ad_payload(fields, adgroup_id, spark_ref, spark))
                        if spark_ref:
                            tiktok_api.create_spark_ad(acct.access_token, acct.advertiser_id, ad_payload)
                        else:
                            tiktok_api.create_ad(acct.access_token, acct.advertiser_id, ad_payload)
                        ad_created = True

        if spark:
            spark.use_count = (spark.use_count or 0) + 1
            spark.last_used_at = datetime.now(timezone.utc)
        log.ok = True
        # instant visibility: seed the campaign cache so the Campaigns page
        # shows the new campaign immediately (the next sweep fills in metrics)
        if log.campaign_id and not (db.query(models.CampaignRecord)
                                    .filter_by(advertiser_id=acct.advertiser_id,
                                               campaign_id=log.campaign_id).first()):
            mode = fields.get("campaign_budget_mode") or "ABO"
            db.add(models.CampaignRecord(
                advertiser_id=acct.advertiser_id, campaign_id=log.campaign_id,
                campaign_name=created_name or fields.get("template_name", ""),
                objective_type=fields.get("objective_type", ""),
                operation_status="ENABLE",
                budget=float(fields.get("campaign_budget") or 0) if mode != "ABO" else 0.0,
                budget_mode=mode if mode != "ABO" else "",
                is_smart_plus=bool(fields.get("smart_plus")),
            ))
        live_log.push("launch", f"Launched '{fields['template_name']}' on {acct.advertiser_name or acct.advertiser_id}")
    except SparkResolveError as e:
        log.ok = False
        log.error_code = "SPARK"
        log.error_message = str(e)
        log.error_technical = getattr(e, "technical", "") or str(e)
    except AssetResolveError as e:
        log.ok = False
        log.error_code = "ASSET"
        log.error_message = str(e)
        log.error_technical = str(e)
    except ConfigError as e:
        log.ok = False
        log.error_code = "CONFIG"
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
    # reserved pool assets go back when NO ad was created (per-account upload &
    # identity caches are kept, so a retry is instant and duplicate-free).
    # A creative shared across a group (allow_creative_reuse) is NOT freed on one
    # account's failure — its other accounts may already have launched with it.
    if not log.ok and not creative_committed:
        reuse = bool(fields.get("allow_creative_reuse"))
        to_free = [pool_text]
        if not reuse:
            to_free.append(creative)
            to_free.append(carousel)
        to_free.extend(sc_creatives)      # Smart Creative: free every reserved video…
        to_free.extend(sc_texts)          # …and text
        for reserved in to_free:
            if reserved is not None:
                reserved.status = "available"
                reserved.used_advertiser_id = ""
                reserved.used_at = None
    # orphan cleanup: if THIS launch made a campaign but no ad ever got created
    # (e.g. the ad group was rejected), delete the empty campaign so failed
    # launches never leave junk shells behind on the account.
    if not log.ok and new_campaign_id and not ad_created and not fields.get("smart_plus"):
        try:
            tiktok_api.delete_campaigns(acct.access_token, acct.advertiser_id, [new_campaign_id])
            log.campaign_id = ""      # it no longer exists — don't show it as tool-launched
        except tiktok_api.TikTokError:
            pass                      # best-effort; a leftover shell is harmless
    db.add(log)
    db.commit()
    return log


def _launch_pace(db: Session) -> float:
    from ..settings_store import get_settings
    try:
        return max(float(get_settings(db).get("launch_pace_sec") or 0), 0.0)
    except (TypeError, ValueError):
        return 1.0


def _remember_batch(db: Session, batch_ref: str, fields: dict) -> None:
    """Persist a batch's launch recipe so failed accounts can be retried with the
    exact same configuration (objective, spark/library, duplication, etc.)."""
    import json as _json
    try:
        queries.set_setting(db, f"batch_fields:{batch_ref}",
                            _json.dumps({k: v for k, v in fields.items()
                                         if k not in ("creative_id", "allow_creative_reuse")}))
    except (TypeError, ValueError):
        pass


def run_batch(db: Session, accounts: list[models.AdAccount], fields: dict) -> str:
    import time as _time

    from .. import rules as rules_mod
    batch_ref = error_messages.new_ref()
    _remember_batch(db, batch_ref, fields)
    pace = _launch_pace(db)
    for i, acct in enumerate(accounts):
        if i and pace:
            _time.sleep(pace)          # spread create-calls — rate-limit safety at scale
        log = launch_to_account(db, acct, fields, batch_ref)
        if log.error_code not in ("ASSET", "CONFIG"):   # preset problems, not account health
            rules_mod.record_launch_outcome(db, acct, log.ok)
    db.commit()
    return batch_ref


def assign_creatives(accounts: list, creatives: list, per_creative: int) -> list:
    """Map accounts → creative ids: each creative covers `per_creative` accounts
    in order (1 creative per N accounts). Returns [(account, creative_id_or_None)];
    accounts beyond the available creatives get None (they'll fail cleanly with a
    'no creatives left' message so nothing launches without one)."""
    per_creative = max(int(per_creative or 1), 1)
    pairs = []
    for i, acct in enumerate(accounts):
        ci = i // per_creative
        cid = creatives[ci].id if ci < len(creatives) else None
        pairs.append((acct, cid))
    return pairs


def run_batch_assigned(db: Session, pairs: list, base_fields: dict) -> str:
    """Launch each (account, creative_id) with that specific creative, reusing a
    creative across its group of accounts (single-use is relaxed for this path)."""
    import time as _time

    from .. import rules as rules_mod
    batch_ref = error_messages.new_ref()
    _remember_batch(db, batch_ref, {**base_fields, "creative_source": "library"})
    pace = _launch_pace(db)
    for i, (acct, cid) in enumerate(pairs):
        if i and pace:
            _time.sleep(pace)
        fields = dict(base_fields)
        fields["creative_source"] = "library"
        if cid is not None:
            fields["creative_id"] = cid
            fields["allow_creative_reuse"] = True
        else:
            fields["creative_id"] = -1     # forces the "no creative" refusal
        log = launch_to_account(db, acct, fields, batch_ref)
        if log.error_code not in ("ASSET", "CONFIG"):
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
    creatives = (db.query(models.Creative).filter_by(status="available", kind="video")
                 .order_by(models.Creative.name).all())
    carousels = (db.query(models.Creative).filter_by(status="available", kind="carousel")
                 .order_by(models.Creative.name).all())
    return render(request, "campaign_launch.html", {
        "templates": templates, "accounts": accounts, "sparks": sparks,
        "creatives": creatives, "carousels": carousels,
        "err": request.query_params.get("err", ""),
        "title": "Create Campaign",
    })


@router.post("/campaigns/launch")
async def launch_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    template_id = str(form.get("template_id") or "")
    spark_code_id = str(form.get("spark_code_id") or "")
    creative_id = str(form.get("creative_id") or "")
    # one select per account row (the ＋ button adds rows) — dedupe, keep order
    seen: set[str] = set()
    advertiser_ids: list[str] = []
    for v in form.getlist("advertiser_ids"):
        v = str(v)
        if v and v not in seen:
            seen.add(v)
            advertiser_ids.append(v)
    template = db.get(models.Template, int(template_id)) if template_id.isdigit() else None
    accts = []
    if advertiser_ids:
        by_id = {a.advertiser_id: a for a in
                 db.query(models.AdAccount)
                 .filter(models.AdAccount.advertiser_id.in_(advertiser_ids)).all()}
        accts = [by_id[i] for i in advertiser_ids if i in by_id]
    if not template or not accts:
        return RedirectResponse("/campaigns/launch?err=missing", status_code=303)
    if spark_code_id and creative_id:
        return RedirectResponse(
            "/campaigns/launch?err=Pick+a+spark+code+OR+a+library+creative+—+not+both.",
            status_code=303)
    if creative_id and len(accts) > 1:
        return RedirectResponse(
            "/campaigns/launch?err=A+specific+creative+launches+ONCE+—+pick+a+single+"
            "account+with+it,+or+use+the+Super+Launcher%27s+library+mode+to+give+every+"
            "account+the+next+unused+creative.", status_code=303)
    overrides: dict = {}
    if spark_code_id:
        overrides["spark_code_id"] = int(spark_code_id)
        # a spark pick at launch time wins over a library preset
        overrides["creative_source"] = "spark"
        overrides["ad_text_mode"] = "fixed"    # pool texts are library-only
    elif creative_id:
        # a library pick wins over a spark preset (video or carousel, by kind)
        overrides["creative_id"] = int(creative_id)
        picked = db.get(models.Creative, int(creative_id))
        overrides["creative_source"] = "carousel" if (picked and picked.kind == "carousel") else "library"
    fields = launch_mod.synthesize(template, overrides)
    batch_ref = run_batch(db, accts, fields)
    return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)


@router.get("/campaigns/result/{batch_ref}")
def launch_result(request: Request, batch_ref: str, db: Session = Depends(get_db)):
    logs = (db.query(models.LaunchLog).filter_by(batch_ref=batch_ref)
            .order_by(models.LaunchLog.id).all())
    ok = sum(1 for l in logs if l.ok)
    import json as _json
    has_recipe = bool(queries.get_setting(db, f"batch_fields:{batch_ref}", ""))
    return render(request, "launch_result.html", {
        "logs": logs, "batch_ref": batch_ref, "ok_count": ok,
        "fail_count": len(logs) - ok, "can_retry": has_recipe,
        "title": f"Launch result · {batch_ref}",
    })


@router.post("/campaigns/result/{batch_ref}/retry")
def retry_failed(request: Request, batch_ref: str, db: Session = Depends(get_db)):
    """Re-launch just the accounts that failed in this batch, reusing the exact
    same recipe. Runs as a fresh batch (with pacing + backoff) so rate-limited
    stragglers get another clean shot."""
    import json as _json
    raw = queries.get_setting(db, f"batch_fields:{batch_ref}", "")
    if not raw:
        return RedirectResponse(f"/campaigns/result/{batch_ref}?note=norecipe", status_code=303)
    try:
        fields = _json.loads(raw)
    except (ValueError, TypeError):
        return RedirectResponse(f"/campaigns/result/{batch_ref}?note=norecipe", status_code=303)
    failed_ids = [l.advertiser_id for l in
                  db.query(models.LaunchLog).filter_by(batch_ref=batch_ref)
                  .filter(models.LaunchLog.ok == False)]                 # noqa: E712
    failed_ids = list(dict.fromkeys(failed_ids))                          # dedupe, keep order
    if not failed_ids:
        return RedirectResponse(f"/campaigns/result/{batch_ref}?note=nofail", status_code=303)
    by_id = {a.advertiser_id: a for a in db.query(models.AdAccount)
             .filter(models.AdAccount.advertiser_id.in_(failed_ids)).all()}
    accounts = [by_id[i] for i in failed_ids if i in by_id]
    if not accounts:
        return RedirectResponse(f"/campaigns/result/{batch_ref}?note=nofail", status_code=303)
    new_ref = run_batch(db, accounts, fields)
    return RedirectResponse(f"/campaigns/result/{new_ref}", status_code=303)


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
                        campaign_name: str = Form(""),
                        campaign_budget: str = Form(""),
                        adgroup_budget_all: str = Form(""),
                        cost_cap_all: str = Form(""),
                        db: Session = Depends(get_db)):
    """Apply whichever fields were filled: campaign name, CBO campaign budget,
    all ad-group budgets, and/or all cost caps."""
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

    rec = (db.query(models.CampaignRecord)
           .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
    new_name = campaign_name.strip()
    src_mode = get_settings(db).get("source_mode", "campaign")
    if new_name and src_mode == "campaign":
        new_name = url_safe_name(new_name)    # the name IS the ?source= value
    old_name = (rec.campaign_name if rec else "") or ""
    if new_name and new_name != old_name:
        try:
            tiktok_api.update_campaign_name(
                acct.access_token, advertiser_id, campaign_id, new_name,
                smart_plus=bool(rec.is_smart_plus) if rec else False)
            changed.append(f"name → {new_name[:40]}")
            if rec:
                rec.campaign_name = new_name
            if src_mode == "campaign":
                # TikTok will now substitute the NEW name — move the join key with
                # it, and re-key the revenue already booked under the old name so
                # the campaign's history stays attributed.
                for lg in db.query(models.LaunchLog).filter_by(campaign_id=campaign_id):
                    if (lg.source or "") in ("", old_name):
                        lg.source = new_name
                if old_name:
                    (db.query(models.PostbackEvent).filter_by(source=old_name)
                       .update({"source": new_name}))
        except tiktok_api.TikTokError as e:
            errors.append(f"rename: code {e.code} {e.message[:80]}")

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
                           next: str = Form("/status"),
                           db: Session = Depends(get_db)):
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    err = ""
    if acct:
        rec = (db.query(models.CampaignRecord)
               .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
        try:
            # Smart+ campaigns have their own status endpoint
            if rec is not None and rec.is_smart_plus:
                tiktok_api.smart_plus_campaign_status_update(
                    acct.access_token, advertiser_id, [campaign_id], operation_status)
            else:
                tiktok_api.update_campaign_status(acct.access_token, advertiser_id,
                                                  [campaign_id], operation_status)
            if rec:
                rec.operation_status = operation_status
                db.commit()
        except tiktok_api.TikTokError as e:
            err = f"code {e.code}: {e.message[:100]}"
    else:
        err = "account not found"
    # only ever redirect within the app (next comes from our own hidden input)
    target = next if next.startswith("/") and not next.startswith("//") else "/status"
    if err:
        sep = "&" if "?" in target else "?"
        target = f"{target}{sep}err={err.replace(' ', '+')}"
    return RedirectResponse(target, status_code=303)
