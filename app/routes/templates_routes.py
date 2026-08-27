"""Presets ("templates") CRUD — parse the big campaign form into a Template row.

⚠ §9.1 — `campaign_budget_mode` and `campaign_budget` are written to the
Template COLUMNS. They must never live inside the `adgroup_settings` JSON blob,
or every preset silently reverts to ABO on save.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..templating import render
from .launch import (
    BID_STRATEGY_OPTIONS, CLICK_ATTR_OPTIONS, CTA_OPTIONS, DESTINATIONS,
    NETWORK_OPTIONS, OBJECTIVES, OPT_GOAL_OPTIONS, OS_OPTIONS, PACING_OPTIONS,
    PIXEL_EVENTS, SPECIAL_INDUSTRIES, SPENDING_POWER_OPTIONS, VIEW_ATTR_OPTIONS,
)

router = APIRouter()


def parse_form(form) -> dict:
    """HTML form -> (top-level column values, adgroup_settings blob)."""
    def val(key, default=""):
        return (form.get(key) or default).strip() if isinstance(form.get(key), str) else (form.get(key) or default)

    top = {
        "name": val("name") or "Untitled preset",
        "objective_type": val("objective_type", "TRAFFIC"),
        # CBO on the ROW (§9.1):
        "campaign_budget_mode": val("campaign_budget_mode", "ABO"),
        "campaign_budget": float(val("campaign_budget") or 0) or None,
        "campaign_name_pattern": val("campaign_name_pattern"),
    }
    if top["campaign_budget_mode"] == "ABO":
        top["campaign_budget"] = None

    ladder_raw = val("cost_cap_ladder")
    ladder = []
    for part in str(ladder_raw).replace(",", " ").split():
        try:
            ladder.append(float(part))
        except ValueError:
            pass

    def id_list(key):
        return [x for x in str(val(key)).replace(",", " ").split() if x]

    def multi(key):
        return form.getlist(key) if hasattr(form, "getlist") else []

    blob = {
        "destination_type": val("destination_type", "website"),
        "adgroup_budget": float(val("adgroup_budget") or 20),
        "adgroup_budget_mode": val("adgroup_budget_mode", "BUDGET_MODE_DAY"),
        "duplicates": int(val("duplicates") or 1),
        "cost_cap_ladder": ladder,
        "location_ids": id_list("location_ids"),
        "gender": val("gender", "GENDER_UNLIMITED"),
        "age_groups": multi("age_groups"),
        "schedule_type": val("schedule_type", "SCHEDULE_FROM_NOW"),
        "schedule_start_time": val("schedule_start_time").replace("T", " "),
        "schedule_end_time": val("schedule_end_time").replace("T", " "),
        "landing_page_url": val("landing_page_url"),
        "instant_page_name": val("instant_page_name"),
        "lead_form_name": val("lead_form_name"),
        "pixel_code": val("pixel_code"),
        "pixel_id": val("pixel_id"),
        "optimization_event": val("optimization_event"),
        "optimization_goal": val("optimization_goal"),
        "creative_source": val("creative_source", "spark"),    # spark | library
        "identity_mode": val("identity_mode", "fixed"),        # fixed | pool
        "ad_text_mode": val("ad_text_mode", "fixed"),          # fixed | pool
        "spark_code_id": int(val("spark_code_id")) if val("spark_code_id") else None,
        "ad_text": val("ad_text"),
        "call_to_action": val("call_to_action", "LEARN_MORE"),
        # auto-pick policy: which accounts qualify when the launcher picks for you
        "account_policy": val("account_policy", "new_only"),   # new_only | reuse
        # -- full Ads-Manager surface ------------------------------------------
        "special_industries": multi("special_industries"),
        "placement_auto": val("placement_mode") == "auto",
        "languages": id_list("languages"),
        "spending_power": val("spending_power"),
        "interest_category_ids": id_list("interest_category_ids"),
        "audience_ids": id_list("audience_ids"),
        "excluded_audience_ids": id_list("excluded_audience_ids"),
        "operating_systems": val("operating_systems"),
        "network_types": multi("network_types"),
        "pacing": val("pacing", "PACING_MODE_SMOOTH"),
        "click_attribution_window": val("click_attribution_window"),
        "view_attribution_window": val("view_attribution_window"),
        # -- bid strategy + Smart+ + advanced settings --
        "bid_strategy": val("bid_strategy"),                   # "" | cost_cap
        "smart_plus": val("smart_plus") == "1",
        "comment_disabled": val("comment_disabled") == "1",
        "video_download_disabled": val("video_download_disabled") == "1",
        "share_disabled": val("share_disabled") == "1",
    }
    # (a cost-cap strategy with no cap values is caught at launch with a clear
    # error rather than silently rewritten here)
    return {"top": top, "blob": blob}


def _form_ctx(db: Session) -> dict:
    return {
        "objectives": OBJECTIVES,
        "destinations": DESTINATIONS,
        "pixel_events": PIXEL_EVENTS,
        "cta_options": CTA_OPTIONS,
        "opt_goal_options": OPT_GOAL_OPTIONS,
        "pacing_options": PACING_OPTIONS,
        "click_attr_options": CLICK_ATTR_OPTIONS,
        "view_attr_options": VIEW_ATTR_OPTIONS,
        "network_options": NETWORK_OPTIONS,
        "os_options": OS_OPTIONS,
        "spending_power_options": SPENDING_POWER_OPTIONS,
        "special_industries": SPECIAL_INDUSTRIES,
        "bid_strategy_options": BID_STRATEGY_OPTIONS,
        "pixels": db.query(models.PixelRecord)
                    .order_by(models.PixelRecord.pixel_name).all(),
        "sparks": db.query(models.SparkCode).filter_by(status="active")
                    .order_by(models.SparkCode.name).all(),
        # pages/forms deduped BY NAME with per-name account counts — the preset
        # stores the name; launches resolve each account's own copy
        "instant_pages": _assets_by_name(db.query(models.InstantPage).all()),
        "lead_forms": _assets_by_name(db.query(models.LeadForm).all()),
    }


def _assets_by_name(rows) -> list[dict]:
    by_name: dict[str, int] = {}
    for r in rows:
        name = (r.name or "").strip()
        if name:
            by_name[name] = by_name.get(name, 0) + 1
    return [{"name": n, "count": c} for n, c in sorted(by_name.items())]


@router.get("/presets")
def list_presets(request: Request, db: Session = Depends(get_db)):
    presets = db.query(models.Template).order_by(models.Template.name).all()
    rows = []
    for p in presets:
        try:
            blob = json.loads(p.adgroup_settings or "{}")
        except json.JSONDecodeError:
            blob = {}
        rows.append({"t": p, "blob": blob})
    return render(request, "templates_list.html", {"rows": rows, "title": "Presets"})


@router.get("/presets/new")
def new_preset(request: Request, db: Session = Depends(get_db)):
    return render(request, "template_form.html", {
        "t": None, "blob": {}, "title": "New preset", **_form_ctx(db)})


@router.get("/presets/{preset_id}/edit")
def edit_preset(request: Request, preset_id: int, db: Session = Depends(get_db)):
    t = db.get(models.Template, preset_id)
    if not t:
        return RedirectResponse("/presets", status_code=303)
    try:
        blob = json.loads(t.adgroup_settings or "{}")
    except json.JSONDecodeError:
        blob = {}
    return render(request, "template_form.html", {
        "t": t, "blob": blob, "title": f"Edit · {t.name}", **_form_ctx(db)})


@router.post("/presets/save")
async def save_preset(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    parsed = parse_form(form)
    preset_id = form.get("preset_id")
    t = db.get(models.Template, int(preset_id)) if preset_id else None
    if not t:
        t = models.Template()
        db.add(t)
    for k, v in parsed["top"].items():
        setattr(t, k, v)
    t.adgroup_settings = json.dumps(parsed["blob"])
    db.commit()
    return RedirectResponse("/presets?ok=saved", status_code=303)


@router.post("/presets/{preset_id}/delete")
def delete_preset(preset_id: int, db: Session = Depends(get_db)):
    t = db.get(models.Template, preset_id)
    if t:
        db.delete(t)
        db.commit()
    return RedirectResponse("/presets?ok=deleted", status_code=303)


@router.post("/presets/{preset_id}/duplicate")
def duplicate_preset(preset_id: int, db: Session = Depends(get_db)):
    t = db.get(models.Template, preset_id)
    if t:
        copy = models.Template(
            name=f"{t.name} (copy)",
            objective_type=t.objective_type,
            campaign_budget_mode=t.campaign_budget_mode,
            campaign_budget=t.campaign_budget,
            campaign_name_pattern=t.campaign_name_pattern,
            adgroup_settings=t.adgroup_settings,
        )
        db.add(copy)
        db.commit()
    return RedirectResponse("/presets", status_code=303)
