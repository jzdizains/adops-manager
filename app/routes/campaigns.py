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
    name = (fields["campaign_name_pattern"]
            .replace("{account}", acct.advertiser_name or acct.advertiser_id)
            .replace("{date}", now.strftime("%m%d")))
    if "{time}" in name:
        name = name.replace("{time}", now.strftime("%H%M%S"))
    else:
        name = f"{name} · {now.strftime('%H%M%S')}"
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
        "call_to_action_list": [{"call_to_action": fields["call_to_action"]}],
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


def _upload_creative_to_account(db: Session, acct: models.AdAccount,
                                creative: models.Creative) -> tuple[str, str]:
    """Upload the creative's video (and its auto cover) into THIS account's
    asset library. Cached so retries never re-upload. Returns (video_id, cover_id)."""
    cached = (db.query(models.CreativeUpload)
              .filter_by(creative_id=creative.id,
                         advertiser_id=acct.advertiser_id).first())
    if cached and cached.video_id:
        return cached.video_id, cached.cover_image_id
    up = tiktok_api.upload_video_file(acct.access_token, acct.advertiser_id,
                                      creative.file_path,
                                      f"c{creative.id}_{creative.file_name}"[:100])
    video_id = str(up.get("video_id", ""))
    if not video_id:
        raise tiktok_api.TikTokError("APP", "video upload returned no video_id")
    poster = up.get("video_cover_url") or up.get("poster_url") or ""
    cover_image_id = ""
    if poster:
        try:
            img = tiktok_api.upload_image_by_url(acct.access_token, acct.advertiser_id,
                                                 poster, f"cover_c{creative.id}")
            cover_image_id = str(img.get("image_id", ""))
        except tiktok_api.TikTokError:
            pass  # cover is best-effort; TikTok can also pick one
    db.add(models.CreativeUpload(creative_id=creative.id,
                                 advertiser_id=acct.advertiser_id,
                                 video_id=video_id, cover_image_id=cover_image_id))
    db.commit()
    return video_id, cover_image_id


def build_library_ad_payload(fields: dict, adgroup_id: str, identity: dict,
                             video_id: str, cover_image_id: str) -> dict:
    creative: dict = {
        "ad_name": f"{fields['template_name']} ad"[:512],
        "ad_format": "SINGLE_VIDEO",
        "ad_text": fields["ad_text"],
        "call_to_action": fields["call_to_action"],
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
    camp = tiktok_api.smart_plus_campaign_create(
        acct.access_token, acct.advertiser_id, build_spc_campaign_payload(fields, acct))
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
    return campaign_id


# ---------------------------------------------------------------------------
# Launch to ONE account (campaign → ad groups → ads) with error capture
# ---------------------------------------------------------------------------

def launch_to_account(db: Session, acct: models.AdAccount, fields: dict, batch_ref: str) -> models.LaunchLog:
    log = models.LaunchLog(
        batch_ref=batch_ref, advertiser_id=acct.advertiser_id,
        advertiser_name=acct.advertiser_name,
        template_id=fields.get("template_id"), template_name=fields.get("template_name", ""))
    creative: models.Creative | None = None      # library creative (reserved below)
    pool_text: models.AdText | None = None
    creative_committed = False                    # True once an ad actually exists
    try:
        # -- config validation FIRST (free, local — before any API calls) ------
        use_library = fields.get("creative_source") == "library"
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

        # source wiring: the spark's source rides the landing URL (P&L join key)
        if spark and (spark.source or "").strip():
            settings = get_settings(db)
            log.spark_code_id = spark.id
            log.source = spark.source.strip()
            if fields.get("landing_page_url"):
                fields = dict(fields)
                fields["landing_page_url"] = apply_source_to_url(
                    fields["landing_page_url"], settings["url_param"], log.source)

        # creative library: reserve the next unused creative, then upload it and
        # the ad identity into THIS account — all before creating anything
        creative_video_id = creative_cover_id = ""
        creative_identity: dict = {}
        if use_library:
            settings = get_settings(db)
            creative = (db.query(models.Creative).filter_by(status="available")
                        .order_by(models.Creative.id).first())
            if not creative:
                raise ConfigError("No available creatives left in the library — upload "
                                  "more on the Creatives page (each creative is used once).")
            # reserve immediately so a concurrent launch can't take the same one
            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            creative.status = "used"
            creative.used_advertiser_id = acct.advertiser_id
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
            if (creative.source or "").strip():
                log.source = creative.source.strip()
                fields = dict(fields)
                fields["landing_page_url"] = apply_source_to_url(
                    fields["landing_page_url"], settings["url_param"], log.source)
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

        if fields.get("smart_plus"):
            log.campaign_id = _launch_smart_plus(acct, fields, spark_ref, spark, pixel_id)
        else:
            camp = tiktok_api.create_campaign(acct.access_token, acct.advertiser_id,
                                              build_campaign_payload(fields, acct))
            campaign_id = str(camp.get("campaign_id"))
            log.campaign_id = campaign_id

            # duplicated ad groups + cost-cap ladder
            ladder = [float(x) for x in fields.get("cost_cap_ladder") or []]
            n = max(int(fields.get("duplicates") or 1), 1)
            plan: list[float | None] = ladder if ladder else [None] * n
            for i, bid in enumerate(plan):
                ag_payload = build_adgroup_payload(fields, acct, campaign_id, i, bid, pixel_id)
                try:
                    ag = tiktok_api.create_adgroup(
                        acct.access_token, acct.advertiser_id, ag_payload)
                except tiktok_api.TikTokError as e:
                    # some accounts now REQUIRE an end time even for daily budgets —
                    # retry once with an explicit 1-year window
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
                adgroup_id = str(ag.get("adgroup_id"))
                if creative is not None:
                    ad_payload = build_library_ad_payload(
                        fields, adgroup_id, creative_identity,
                        creative_video_id, creative_cover_id)
                    tiktok_api.create_ad(acct.access_token, acct.advertiser_id, ad_payload)
                    creative_committed = True
                    if not creative.used_campaign_id:
                        creative.used_campaign_id = campaign_id
                    if pool_text is not None and not pool_text.used_campaign_id:
                        pool_text.used_campaign_id = campaign_id
                elif spark_ref or fields.get("landing_page_url") or fields.get("instant_page_id") \
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
    # identity caches are kept, so a retry is instant and duplicate-free)
    if not log.ok and not creative_committed:
        for reserved in (creative, pool_text):
            if reserved is not None:
                reserved.status = "available"
                reserved.used_advertiser_id = ""
                reserved.used_at = None
    db.add(log)
    db.commit()
    return log


def run_batch(db: Session, accounts: list[models.AdAccount], fields: dict) -> str:
    from .. import rules as rules_mod
    batch_ref = error_messages.new_ref()
    for acct in accounts:
        log = launch_to_account(db, acct, fields, batch_ref)
        if log.error_code not in ("ASSET", "CONFIG"):   # preset problems, not account health
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
    if new_name and new_name != ((rec.campaign_name if rec else "") or ""):
        try:
            tiktok_api.update_campaign_name(
                acct.access_token, advertiser_id, campaign_id, new_name,
                smart_plus=bool(rec.is_smart_plus) if rec else False)
            changed.append(f"name → {new_name[:40]}")
            if rec:
                rec.campaign_name = new_name
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
