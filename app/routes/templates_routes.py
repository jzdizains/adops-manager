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
from .launch import DESTINATIONS, OBJECTIVES, PIXEL_EVENTS

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

    blob = {
        "destination_type": val("destination_type", "website"),
        "adgroup_budget": float(val("adgroup_budget") or 20),
        "duplicates": int(val("duplicates") or 1),
        "cost_cap_ladder": ladder,
        "location_ids": [x for x in str(val("location_ids")).replace(",", " ").split() if x],
        "gender": val("gender", "GENDER_UNLIMITED"),
        "age_groups": form.getlist("age_groups") if hasattr(form, "getlist") else [],
        "schedule_type": val("schedule_type", "SCHEDULE_FROM_NOW"),
        "schedule_start_time": val("schedule_start_time"),
        "landing_page_url": val("landing_page_url"),
        "instant_page_id": val("instant_page_id"),
        "lead_form_id": val("lead_form_id"),
        "pixel_code": val("pixel_code"),
        "pixel_id": val("pixel_id"),
        "optimization_event": val("optimization_event"),
        "spark_code_id": int(val("spark_code_id")) if val("spark_code_id") else None,
        "ad_text": val("ad_text"),
        "call_to_action": val("call_to_action", "LEARN_MORE"),
    }
    return {"top": top, "blob": blob}


def _form_ctx(db: Session) -> dict:
    return {
        "objectives": OBJECTIVES,
        "destinations": DESTINATIONS,
        "pixel_events": PIXEL_EVENTS,
        "sparks": db.query(models.SparkCode).filter_by(status="active")
                    .order_by(models.SparkCode.name).all(),
        "instant_pages": db.query(models.InstantPage).order_by(models.InstantPage.name).all(),
        "lead_forms": db.query(models.LeadForm).order_by(models.LeadForm.name).all(),
    }


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
