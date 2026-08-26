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
        "duplicates": int(s.get("duplicates") or 1),
        "optimization_goal": s.get("optimization_goal") or opt_goal,
        "billing_event": s.get("billing_event") or billing_event,
        "bid_type": s.get("bid_type") or bid_type,
        "cost_cap_ladder": s.get("cost_cap_ladder") or [],       # list of bid prices
        "location_ids": s.get("location_ids") or [],
        "gender": s.get("gender") or "GENDER_UNLIMITED",
        "age_groups": s.get("age_groups") or [],
        "schedule_type": s.get("schedule_type") or "SCHEDULE_FROM_NOW",
        "schedule_start_time": s.get("schedule_start_time") or "",
        # -- destination detail --
        "landing_page_url": s.get("landing_page_url") or "",
        "instant_page_id": s.get("instant_page_id") or "",
        "lead_form_id": s.get("lead_form_id") or "",
        "pixel_code": s.get("pixel_code") or "",
        "pixel_id": s.get("pixel_id") or "",                     # numeric, if already resolved
        "optimization_event": s.get("optimization_event") or "",
        # -- creative / spark --
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
        return "Instant page"
    if d == "lead_form":
        return "Lead form"
    return "Website"
