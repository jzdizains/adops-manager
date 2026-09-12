"""Duplicate an ad group — settings and its ads — inside the same campaign.

TikTok has no copy endpoint for ad groups (the Marketing API exposes create, get,
update, status/update and quota, and nothing else). A duplicate is therefore a READ of
the source followed by a fresh CREATE, which raises the only interesting problem here:
/adgroup/get/ returns many fields that /adgroup/create/ will not accept — ids, timestamps,
delivery state, computed values. Sending one back is a 40002.

So the payload is built from an ALLOWLIST taken from TikTok's own AdgroupCreateBody and
AdcreateCreatives schemas, not by stripping the read-only fields we happen to think of. A
new field TikTok adds is then ignored until it is added here, which is the safe direction
to be wrong in: a missing optional field makes a slightly different ad group, a rejected
field makes no ad group at all.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models, tiktok_api

log = logging.getLogger("adops.adgroup_copy")

MAX_COPIES = 20          # one click must not be able to create a hundred live ad groups

# Every field /adgroup/create/ accepts (TikTok's AdgroupCreateBody), minus the ones this
# code sets itself: advertiser_id, adgroup_name, request_id.
ADGROUP_FIELDS = {
    "actions", "adgroup_app_profile_page_state", "age_groups", "app_config", "app_id",
    "attribution_event_count", "audience_ids", "audience_rule", "audience_type",
    "automated_keywords_enabled", "bid_display_mode", "bid_price", "bid_type",
    "billing_event", "blocked_pangle_app_ids", "brand_safety_partner", "brand_safety_type",
    "budget", "budget_mode", "campaign_id", "carrier_ids", "catalog_authorized_bc_id",
    "catalog_id", "category_exclusion_ids", "click_attribution_window", "comment_disabled",
    "contextual_tag_ids", "conversion_bid_price", "creative_material_mode",
    "custom_conversion_id", "dayparting", "deep_bid_type", "deep_cpa_bid",
    "deep_funnel_event_source", "deep_funnel_event_source_id",
    "deep_funnel_optimization_event", "deep_funnel_optimization_status", "device_model_ids",
    "device_price_ranges", "engaged_view_attribution_window", "excluded_audience_ids",
    "excluded_custom_actions", "excluded_pangle_audience_package_ids", "frequency",
    "frequency_schedule", "gender", "household_income", "identity_authorized_bc_id",
    "identity_id", "identity_type", "included_custom_actions",
    "included_pangle_audience_package_ids", "interest_category_ids", "interest_keyword_ids",
    "ios14_targeting", "is_hfss", "is_lhf_compliance", "isp_ids", "languages",
    "location_ids", "message_event_set_id", "messaging_app_account_id", "messaging_app_type",
    "min_android_version", "min_ios_version", "network_types", "next_day_retention",
    "operating_systems", "operation_status", "optimization_event", "optimization_goal",
    "pacing", "phone_number", "phone_region_calling_code", "phone_region_code", "pixel_id",
    "placement_type", "placements", "product_source", "promotion_target_type",
    "promotion_type", "promotion_website_type", "purchase_intention_keyword_ids", "roas_bid",
    "saved_audience_id", "schedule_end_time", "schedule_start_time", "schedule_type",
    "search_keywords", "search_result_enabled", "secondary_optimization_event",
    "share_disabled", "shopping_ads_retargeting_actions_days",
    "shopping_ads_retargeting_custom_audience_relation", "shopping_ads_retargeting_type",
    "shopping_ads_type", "skip_learning_phase", "smart_audience_enabled",
    "smart_interest_behavior_enabled", "spending_power", "statistic_type",
    "store_authorized_bc_id", "store_id", "tiktok_subplacements", "vbo_window",
    "vertical_sensitivity_id", "video_download_disabled", "view_attribution_window",
    "zipcode_ids",
}

# Every field one entry of /ad/create/ `creatives` accepts (TikTok's AdcreateCreatives).
AD_FIELDS = {
    "ad_format", "ad_name", "ad_text", "ad_texts", "aigc_disclosure_type", "app_name",
    "auto_disclaimer_types", "auto_message_id", "avatar_icon_web_uri",
    "brand_safety_postbid_partner", "brand_safety_vast_url", "call_to_action",
    "call_to_action_id", "card_id", "carousel_image_index", "catalog_id",
    "click_tracking_url", "cpp_url", "creative_authorized", "creative_type",
    "dark_post_status", "deeplink", "deeplink_format_type", "deeplink_type",
    "deeplink_utm_params", "disclaimer_clickable_texts", "disclaimer_text",
    "disclaimer_type", "display_name", "dynamic_destination", "dynamic_format",
    "end_card_cta", "fallback_type", "identity_authorized_bc_id", "identity_id",
    "identity_type", "image_ids", "impression_tracking_url", "instant_product_page_used",
    "item_duet_status", "item_group_ids", "item_stitch_status", "landing_page_url",
    "music_id", "operation_status", "page_id", "page_image_index", "phone_number",
    "phone_region_calling_code", "phone_region_code", "playable_url",
    "product_display_field_list", "product_set_id", "product_specific_type",
    "promotional_music_disabled", "schedule_id", "shopping_ads_deeplink_type",
    "shopping_ads_fallback_type", "shopping_ads_video_package_id", "showcase_products",
    "sku_ids", "tiktok_item_id", "tiktok_page_category", "tracking_app_id",
    "tracking_message_event_set_id", "tracking_offline_event_set_ids", "tracking_pixel_id",
    "utm_params", "vehicle_ids", "vertical_video_strategy", "video_id",
    "video_view_tracking_url", "viewability_postbid_partner", "viewability_vast_url",
}


def _keep(src: dict, allowed: set[str]) -> dict:
    """Only the fields TikTok will accept, and only where the source actually has one.
    An empty string or null is not 'the same setting' — it is a different one, and some
    of them are rejected outright."""
    out = {}
    for k, v in (src or {}).items():
        if k in allowed and v is not None and v != "" and v != []:
            out[k] = v
    return out


def _copy_name(name: str, n: int) -> str:
    base = (name or "Ad group").strip()
    suffix = f" (copy {n})" if n > 1 else " (copy)"
    return (base[:512 - len(suffix)] + suffix)[:512]


def list_adgroups(acct: models.AdAccount, campaign_id: str) -> list[dict]:
    """The campaign's ad groups, with how many ads each holds. Read-only."""
    data = tiktok_api.list_adgroups(acct.access_token, acct.advertiser_id, [str(campaign_id)]) or {}
    groups = data.get("list") or []
    counts: dict[str, int] = {}
    try:
        ads = tiktok_api.list_ads(acct.access_token, acct.advertiser_id, page_size=100,
                                  filtering={"campaign_ids": [str(campaign_id)]}) or {}
        for ad in (ads.get("list") or []):
            agid = str(ad.get("adgroup_id") or "")
            if agid:
                counts[agid] = counts.get(agid, 0) + 1
    except tiktok_api.TikTokError as e:
        log.info("ad count unavailable for %s: %s", campaign_id, e)
    out = []
    for g in groups:
        agid = str(g.get("adgroup_id") or "")
        out.append({
            "adgroup_id": agid,
            "name": g.get("adgroup_name") or agid,
            "operation_status": g.get("operation_status") or "",
            "secondary_status": (g.get("secondary_status") or "").replace("ADGROUP_STATUS_", "").replace("_", " ").lower(),
            "budget": float(g.get("budget") or 0),
            "budget_mode": g.get("budget_mode") or "",
            "bid_price": float(g.get("bid_price") or 0),
            "optimization_goal": g.get("optimization_goal") or "",
            "ads": counts.get(agid, 0),
        })
    return out


def _source_adgroup(acct: models.AdAccount, campaign_id: str, adgroup_id: str) -> dict | None:
    data = tiktok_api.list_adgroups(acct.access_token, acct.advertiser_id, [str(campaign_id)]) or {}
    for g in (data.get("list") or []):
        if str(g.get("adgroup_id")) == str(adgroup_id):
            return g
    return None


def _source_ads(acct: models.AdAccount, adgroup_id: str) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 6):
        data = tiktok_api.list_ads(acct.access_token, acct.advertiser_id, page=page, page_size=100,
                                   filtering={"adgroup_ids": [str(adgroup_id)]}) or {}
        batch = data.get("list") or []
        out.extend(batch)
        info = data.get("page_info") or {}
        if len(batch) < 100 or (info.get("total_page") and page >= int(info["total_page"])):
            break
    return out


def _future_start(payload: dict) -> dict:
    """A schedule that started in the past is fine for the original and refused for a new
    ad group. Only touched when TikTok actually complains about it."""
    p = dict(payload)
    start = datetime.now(timezone.utc) + timedelta(minutes=10)
    p["schedule_start_time"] = start.strftime("%Y-%m-%d %H:%M:%S")
    return p


def duplicate(db: Session, acct: models.AdAccount, campaign_id: str, adgroup_id: str,
              copies: int = 1, on_progress=None) -> dict:
    """Make `copies` duplicates of one ad group, each with the source's ads.

    Never raises for a TikTok error: every copy reports its own outcome, so one refusal
    does not hide the copies that worked.
    """
    def say(t: str) -> None:
        if on_progress:
            try:
                on_progress(t)
            except Exception:      # noqa: BLE001
                pass

    copies = max(1, min(int(copies or 1), MAX_COPIES))
    report: dict = {"campaign_id": str(campaign_id), "adgroup_id": str(adgroup_id),
                    "copies": copies, "made": [], "errors": [],
                    "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")}

    say("reading the ad group")
    try:
        src = _source_adgroup(acct, campaign_id, adgroup_id)
    except tiktok_api.TikTokError as e:
        report["error"] = f"could not read the ad group: {e.message} (code {e.code})"
        return report
    if src is None:
        report["error"] = "That ad group is not in this campaign any more — sync and try again."
        return report

    report["source_name"] = src.get("adgroup_name") or str(adgroup_id)
    base = _keep(src, ADGROUP_FIELDS)
    base["campaign_id"] = str(campaign_id)          # same campaign, always

    say("reading its ads")
    try:
        src_ads = _source_ads(acct, adgroup_id)
    except tiktok_api.TikTokError as e:
        src_ads = []
        report["errors"].append(f"could not read the ad group's ads: {e.message} (code {e.code})")
    report["source_ads"] = len(src_ads)

    for n in range(1, copies + 1):
        say(f"creating copy {n} of {copies}")
        payload = dict(base)
        payload["adgroup_name"] = _copy_name(src.get("adgroup_name") or "", n)
        made: dict = {"n": n, "name": payload["adgroup_name"], "adgroup_id": "", "ads": 0,
                      "ad_errors": []}
        try:
            try:
                res = tiktok_api.create_adgroup(acct.access_token, acct.advertiser_id, payload)
            except tiktok_api.TikTokError as e:
                if "schedule_start_time" not in (e.message or ""):
                    raise
                made["note"] = "its start time had passed — the copy starts in 10 minutes"
                res = tiktok_api.create_adgroup(acct.access_token, acct.advertiser_id,
                                                _future_start(payload))
        except tiktok_api.TikTokError as e:
            made["error"] = f"{e.message} (code {e.code})"
            report["made"].append(made)
            report["errors"].append(f"copy {n}: {e.message} (code {e.code})")
            continue

        new_id = str((res or {}).get("adgroup_id") or "")
        made["adgroup_id"] = new_id
        if not new_id:
            made["error"] = "TikTok accepted the ad group but returned no id"
            report["made"].append(made)
            continue

        for ad in src_ads:
            creative = _keep(ad, AD_FIELDS)
            if not creative:
                continue
            say(f"copy {n}: adding “{creative.get('ad_name') or 'ad'}”")
            try:
                tiktok_api.create_ad(acct.access_token, acct.advertiser_id,
                                     {"adgroup_id": new_id, "creatives": [creative]})
                made["ads"] += 1
            except tiktok_api.TikTokError as e:
                made["ad_errors"].append(f"{creative.get('ad_name') or 'ad'}: {e.message} (code {e.code})")
        report["made"].append(made)

    ok = [m for m in report["made"] if m.get("adgroup_id") and not m.get("error")]
    ads_made = sum(m["ads"] for m in ok)
    live = sum(1 for m in ok if (base.get("operation_status") or "ENABLE") == "ENABLE")
    report["summary"] = (
        f"{len(ok)} of {copies} copy(ies) created with {ads_made} ad(s)"
        + (f" · {live} started live and can spend" if live else " · all paused")
        + (f" · {len(report['errors'])} problem(s)" if report["errors"] else ""))
    return report
