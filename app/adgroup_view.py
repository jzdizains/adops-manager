"""What TikTok has stored on an ad group — the settings, as Ads Manager would show them.

One read-only /adgroup/get/ object in, a labelled list out, for the drawer's "Settings"
popover. Everything non-empty is shown: the well-known fields first with plain labels,
then whatever else TikTok returned (that is how a new field surfaces the first time an
Ads-Manager-made ad group carries it). No secrets live on an ad group.
"""
from __future__ import annotations

# (field, label) — in the order Ads Manager walks through them
KNOWN: list[tuple[str, str]] = [
    ("optimization_goal", "Optimisation goal"),
    ("optimization_event", "Optimisation event"),
    ("secondary_optimization_event", "Secondary event"),
    ("pixel_id", "Pixel"),
    ("custom_conversion_id", "Custom conversion"),
    ("promotion_type", "Promotion type"),
    ("promotion_target_type", "Promotion target"),
    ("promotion_website_type", "Website type"),
    ("billing_event", "Billing event"),
    ("bid_type", "Bid strategy"),
    ("bid_price", "Bid"),
    ("conversion_bid_price", "Cost cap"),
    ("deep_bid_type", "Value bidding"),
    ("roas_bid", "Min ROAS"),
    ("budget_mode", "Budget mode"),
    ("budget", "Budget"),
    ("pacing", "Delivery type"),
    ("skip_learning_phase", "Skip learning phase"),
    ("schedule_type", "Schedule"),
    ("schedule_start_time", "Start"),
    ("schedule_end_time", "End"),
    ("dayparting", "Dayparting"),
    ("placement_type", "Placement mode"),
    ("placements", "Placements"),
    ("tiktok_subplacements", "Sub-placements"),
    ("search_result_enabled", "Search results"),
    ("comment_disabled", "Comments off"),
    ("video_download_disabled", "Download off"),
    ("share_disabled", "Sharing off"),
    ("brand_safety_type", "Brand safety"),
    ("location_ids", "Locations"),
    ("zipcode_ids", "Zip codes"),
    ("languages", "Languages"),
    ("gender", "Gender"),
    ("age_groups", "Ages"),
    ("spending_power", "Spending power"),
    ("household_income", "Household income"),
    ("operating_systems", "OS"),
    ("min_ios_version", "Min iOS"),
    ("min_android_version", "Min Android"),
    ("device_model_ids", "Device models"),
    ("device_price_ranges", "Device price"),
    ("network_types", "Connection"),
    ("carrier_ids", "Carriers"),
    ("isp_ids", "ISPs"),
    ("interest_category_ids", "Interests"),
    ("interest_keyword_ids", "Interest keywords"),
    ("purchase_intention_keyword_ids", "Purchase intentions"),
    ("audience_ids", "Audiences"),
    ("excluded_audience_ids", "Excluded audiences"),
    ("smart_audience_enabled", "Smart audience"),
    ("smart_interest_behavior_enabled", "Smart interests"),
    ("contextual_tag_ids", "Content topics"),
    ("click_attribution_window", "Click attribution"),
    ("view_attribution_window", "View attribution"),
    ("engaged_view_attribution_window", "Engaged-view attribution"),
    ("attribution_event_count", "Count"),
    ("creative_material_mode", "Creative mode"),
    ("identity_type", "Identity type"),
    ("identity_id", "Identity"),
]
SKIP = {"adgroup_id", "adgroup_name", "advertiser_id", "campaign_id", "campaign_name", "create_time",
        "modify_time", "operation_status", "secondary_status", "is_new_structure", "budget_share_mode",
        "campaign_type", "is_smart_performance_campaign", "campaign_system_origin"}
_EMPTY = (None, "", [], {}, "UNSET")


def _fmt(key: str, v) -> str:
    if key == "dayparting" and isinstance(v, str):
        return "all hours" if set(v) <= {"1"} else f"custom ({v.count('1')} of {len(v)} half-hours)"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, list):
        items = [str(x) for x in v]
        head = ", ".join(items[:8])
        return head + (f" … +{len(items) - 8}" if len(items) > 8 else "") + (f"  ({len(items)})" if len(items) > 3 else "")
    if isinstance(v, dict):
        return ", ".join(f"{k}={val}" for k, val in list(v.items())[:6])
    return str(v)[:120]


def rows(g: dict) -> list[dict]:
    """[{key, label, value}] for every non-empty setting on the ad group."""
    out: list[dict] = []
    seen: set[str] = set()
    for key, label in KNOWN:
        v = g.get(key)
        seen.add(key)
        if v in _EMPTY:
            continue
        out.append({"key": key, "label": label, "value": _fmt(key, v)})
    for key in sorted(g):
        if key in seen or key in SKIP:
            continue
        v = g.get(key)
        if v in _EMPTY:
            continue
        out.append({"key": key, "label": key.replace("_", " "), "value": _fmt(key, v), "other": True})
    return out
