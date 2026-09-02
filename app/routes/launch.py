"""Preset → launch-field synthesis.

Takes a Template row (top-level campaign columns + the adgroup_settings JSON
blob) and produces the flat `fields` dict the launch engine consumes. Also owns
the objective→optimization map.

⚠ §9.1 — campaign_budget_mode / campaign_budget are read from the Template
COLUMNS, never from the JSON blob.
"""
from __future__ import annotations

import json
from typing import Any

from .. import models

# ---------------------------------------------------------------------------
# Objective → optimization mapping
# (objective_type, destination) -> (optimization_goal, billing_event, bid_type)
# ---------------------------------------------------------------------------
OBJECTIVE_MAP: dict[tuple[str, str], tuple[str, str, str]] = {
    ("TRAFFIC", "website"):          ("CLICK",   "CPC",  "BID_TYPE_NO_BID"),
    ("TRAFFIC", "instant_page"):     ("CLICK",   "CPC",  "BID_TYPE_NO_BID"),
    ("WEB_CONVERSIONS", "pixel"):    ("CONVERT", "OCPM", "BID_TYPE_NO_BID"),
    ("WEB_CONVERSIONS", "website"):  ("CONVERT", "OCPM", "BID_TYPE_NO_BID"),
    ("LEAD_GENERATION", "lead_form"): ("CONVERT", "OCPM", "BID_TYPE_NO_BID"),
    ("LEAD_GENERATION", "instant_page"): ("CONVERT", "OCPM", "BID_TYPE_NO_BID"),
    # TikTok moved lead-type pixel events (Complete Registration, Contact) OUT of
    # Website Conversions — they now require the Lead Generation objective with a
    # website destination (web-form flavor): same CONVERT/pixel wiring.
    ("LEAD_GENERATION", "website"):  ("CONVERT", "OCPM", "BID_TYPE_NO_BID"),
    ("LEAD_GENERATION", "pixel"):    ("CONVERT", "OCPM", "BID_TYPE_NO_BID"),
    ("REACH", "website"):            ("REACH",   "CPM",  "BID_TYPE_NO_BID"),
    ("VIDEO_VIEWS", "website"):      ("ENGAGED_VIEW", "CPV", "BID_TYPE_NO_BID"),
}

# Pixel optimization events the preset form offers (§5)
PIXEL_EVENTS = [
    ("ON_WEB_DETAIL", "View content"),
    ("FORM", "Form submit"),
    ("ON_WEB_REGISTER", "Complete registration"),
    ("BUTTON", "Button click"),
    ("ON_WEB_ORDER", "Place order"),
    ("SHOPPING", "Purchase"),
]

OBJECTIVES = ["TRAFFIC", "WEB_CONVERSIONS", "LEAD_GENERATION", "REACH", "VIDEO_VIEWS"]
DESTINATIONS = ["website", "instant_page", "lead_form", "pixel"]

# Objective labels that mirror TikTok Ads Manager's new "Goal" naming, so the
# preset vocabulary matches what the operator sees in TikTok.
# Exactly the wording TikTok Ads Manager shows in its "Optimization goal" list,
# so the preset's goal selector reads the same as what the operator sees on TikTok.
OBJECTIVE_OPTIONS = [
    ("LEAD_GENERATION", "Leads"),
    ("WEB_CONVERSIONS", "Website engagements"),
    ("TRAFFIC", "Click"),
    ("REACH", "Reach"),
    ("VIDEO_VIEWS", "Video views"),
]
GOAL_LABELS = {
    "": "Auto (from objective)", "CONVERT": "Conversion",
    "CLICK": "Click", "TRAFFIC_LANDING_PAGE_VIEW": "Landing page view",
    "REACH": "Reach", "ENGAGED_VIEW": "Focused view (6s)",
}
DEST_LABELS = {
    "website": "Website", "pixel": "Website + pixel",
    "instant_page": "Instant page", "lead_form": "Instant form (lead)",
}
# Which destinations / optimization goals / event-need each objective allows —
# mirrors what Ads Manager offers so the form can only produce valid combos.
#   event: "required" (always needs pixel+event) | "conditional" (only on a
#   website/pixel destination) | "none"
OBJECTIVE_RULES = {
    "WEB_CONVERSIONS": {"destinations": ["website", "pixel"],
                        "goals": ["CONVERT"], "event": "required"},
    "LEAD_GENERATION": {"destinations": ["lead_form", "website", "pixel"],
                        "goals": ["CONVERT"], "event": "conditional"},
    "TRAFFIC":         {"destinations": ["website", "instant_page"],
                        "goals": ["CLICK", "TRAFFIC_LANDING_PAGE_VIEW"], "event": "none"},
    "REACH":           {"destinations": ["website"],
                        "goals": ["REACH"], "event": "none"},
    "VIDEO_VIEWS":     {"destinations": ["website"],
                        "goals": ["ENGAGED_VIEW"], "event": "none"},
}

# Full Ads-Manager option sets (used by the preset form + payload builders)
CTA_AUTO = "AUTO"        # Dynamic CTA: TikTok picks the best from a candidate set per viewer
# candidate set for web objectives (all this tool's objectives are web/lead) — the
# pool TikTok optimises between when the preset's CTA is "Auto"
CTA_AUTO_SET = ["LEARN_MORE", "SHOP_NOW", "SIGN_UP", "SUBSCRIBE", "CONTACT_US", "APPLY_NOW",
                "BOOK_NOW", "GET_QUOTE", "ORDER_NOW", "VISIT_STORE", "READ_MORE", "VIEW_NOW",
                "INTERESTED", "WATCH_NOW"]
CTA_OPTIONS = [
    "LEARN_MORE", "SHOP_NOW", "SIGN_UP", "SUBSCRIBE", "CONTACT_US", "APPLY_NOW",
    "BOOK_NOW", "DOWNLOAD_NOW", "GET_QUOTE", "ORDER_NOW", "PLAY_GAME",
    "VISIT_STORE", "WATCH_NOW", "INTERESTED", "LISTEN_NOW", "READ_MORE",
    "VIEW_NOW", "PRE_ORDER_NOW", "GET_TICKETS_NOW", "EXPERIENCE_NOW",
]
OPT_GOAL_OPTIONS = [
    ("", "Auto (from objective)"),
    ("CONVERT", "Conversions"), ("CLICK", "Clicks"), ("REACH", "Reach"),
    ("ENGAGED_VIEW", "Engaged views"), ("TRAFFIC_LANDING_PAGE_VIEW", "Landing page views"),
]
PACING_OPTIONS = [("PACING_MODE_SMOOTH", "Standard (smooth)"),
                  ("PACING_MODE_FAST", "Accelerated")]
CLICK_ATTR_OPTIONS = [("", "TikTok default"), ("ONE_DAY", "1 day"),
                      ("SEVEN_DAYS", "7 days"), ("FOURTEEN_DAYS", "14 days"),
                      ("TWENTY_EIGHT_DAYS", "28 days")]
VIEW_ATTR_OPTIONS = [("", "TikTok default"), ("OFF", "Off"),
                     ("ONE_DAY", "1 day"), ("SEVEN_DAYS", "7 days")]
NETWORK_OPTIONS = [["WIFI", "WiFi"], ["5G", "5G"], ["4G", "4G"], ["3G", "3G"], ["2G", "2G"]]
OS_OPTIONS = [("", "All"), ("ANDROID", "Android only"), ("IOS", "iOS only")]
SPENDING_POWER_OPTIONS = [("", "All"), ("HIGH", "High spending power")]
SPECIAL_INDUSTRIES = [["HOUSING", "Housing"], ["EMPLOYMENT", "Employment"], ["CREDIT", "Credit"]]
BID_STRATEGY_OPTIONS = [
    ("", "Auto — cost cap when caps are set, else maximum delivery"),
    ("max_delivery", "Maximum delivery (ignores any cost caps)"),
    ("cost_cap", "Cost cap (requires cap value(s) below)"),
]


def optimization_for(objective_type: str, destination: str) -> tuple[str, str, str]:
    key = (objective_type, destination)
    if key in OBJECTIVE_MAP:
        return OBJECTIVE_MAP[key]
    # sane default: click-optimized traffic
    return ("CLICK", "CPC", "BID_TYPE_NO_BID")


def adgroup_settings_of(template: models.Template) -> dict:
    try:
        return json.loads(template.adgroup_settings or "{}")
    except json.JSONDecodeError:
        return {}


def synthesize(template: models.Template, overrides: dict[str, Any] | None = None) -> dict:
    """Template row -> flat launch fields dict. `overrides` come from the
    launcher form (e.g. a different spark code picked at launch time)."""
    s = adgroup_settings_of(template)
    destination = s.get("destination_type", "website")
    opt_goal, billing_event, bid_type = optimization_for(template.objective_type, destination)

    fields: dict[str, Any] = {
        # -- campaign level (from COLUMNS — §9.1) --
        "template_id": template.id,
        "template_name": template.name,
        "objective_type": template.objective_type,
        "campaign_budget_mode": template.campaign_budget_mode,   # ABO | BUDGET_MODE_DAY | BUDGET_MODE_TOTAL
        "campaign_budget": template.campaign_budget,
        "campaign_name_pattern": template.campaign_name_pattern or template.name,
        # -- ad group level (from JSON blob) --
        "destination_type": destination,
        "adgroup_budget": float(s.get("adgroup_budget") or 20.0),
        "duplicates": int(s.get("duplicates") or 1),           # ad groups per campaign
        "ads_per_group": int(s.get("ads_per_group") or 1),      # ads per ad group
        # Smart Creative: TikTok auto-combines several library videos + texts
        "smart_creative": bool(s.get("smart_creative")),
        "smart_creative_videos": int(s.get("smart_creative_videos") or 5),
        "smart_creative_texts": int(s.get("smart_creative_texts") or 5),
        "optimization_goal": s.get("optimization_goal") or opt_goal,
        "billing_event": s.get("billing_event") or billing_event,
        "bid_type": s.get("bid_type") or bid_type,
        "cost_cap_ladder": s.get("cost_cap_ladder") or [],       # list of bid prices
        "location_ids": s.get("location_ids") or [],
        "gender": s.get("gender") or "GENDER_UNLIMITED",
        "age_groups": s.get("age_groups") or [],
        "schedule_type": s.get("schedule_type") or "SCHEDULE_FROM_NOW",
        "schedule_start_time": s.get("schedule_start_time") or "",
        "schedule_end_time": s.get("schedule_end_time") or "",
        # -- full Ads-Manager surface (all optional; omitted from payloads when empty)
        "special_industries": s.get("special_industries") or [],
        "placement_auto": bool(s.get("placement_auto")),
        "search_enabled": bool(s.get("search_enabled")),
        "languages": s.get("languages") or [],
        "spending_power": s.get("spending_power") or "",
        "interest_category_ids": s.get("interest_category_ids") or [],
        "audience_ids": s.get("audience_ids") or [],
        "excluded_audience_ids": s.get("excluded_audience_ids") or [],
        "operating_systems": s.get("operating_systems") or "",
        "network_types": s.get("network_types") or [],
        "adgroup_budget_mode": s.get("adgroup_budget_mode") or "BUDGET_MODE_DAY",
        "pacing": s.get("pacing") or "PACING_MODE_SMOOTH",
        "click_attribution_window": s.get("click_attribution_window") or "",
        "view_attribution_window": s.get("view_attribution_window") or "",
        # -- bid strategy + Smart+ + advanced settings --
        "bid_strategy": s.get("bid_strategy") or "",           # "" = max delivery | cost_cap
        "smart_plus": bool(s.get("smart_plus")),
        "comment_disabled": bool(s.get("comment_disabled")),
        "video_download_disabled": bool(s.get("video_download_disabled")),
        "share_disabled": bool(s.get("share_disabled")),
        # -- destination detail --
        "landing_page_url": s.get("landing_page_url") or "",
        # pages/forms are selected BY NAME — per target account, the engine
        # resolves that account's own copy at launch (per-account asset IDs)
        "instant_page_name": s.get("instant_page_name") or "",
        "lead_form_name": s.get("lead_form_name") or "",
        "instant_page_id": s.get("instant_page_id") or "",   # legacy exact-id
        "lead_form_id": s.get("lead_form_id") or "",         # legacy exact-id
        "pixel_code": s.get("pixel_code") or "",
        "pixel_id": s.get("pixel_id") or "",                     # numeric, if already resolved
        "optimization_event": s.get("optimization_event") or "",
        # -- creative / spark --
        "creative_source": s.get("creative_source") or "spark",   # spark | library
        "ad_text_mode": s.get("ad_text_mode") or "fixed",         # fixed | pool (library only)
        "spark_code_id": s.get("spark_code_id"),
        "ad_text": s.get("ad_text") or "",
        "call_to_action": s.get("call_to_action") or "LEARN_MORE",
        "account_policy": s.get("account_policy") or "new_only",
    }
    if overrides:
        fields.update({k: v for k, v in overrides.items() if v not in (None, "")})
    return fields


def destination_label(fields: dict) -> str:
    """Human label the Super Launcher shows when it auto-locks the destination."""
    d = fields.get("destination_type", "website")
    if d == "pixel":
        ev = dict(PIXEL_EVENTS).get(fields.get("optimization_event", ""), fields.get("optimization_event", ""))
        return f"⚡ Pixel · {ev or 'no event set'}"
    if d == "instant_page":
        return f"Instant page · {fields.get('instant_page_name') or '?'}"
    if d == "lead_form":
        return f"Lead form · {fields.get('lead_form_name') or '?'}"
    return "Website"
