"""Super Launcher — the marquee feature. Pick accounts (checkboxes) + a preset;
the destination auto-locks from the preset; one click launches to all selected
accounts via the launch engine."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..templating import render
from . import campaigns as engine
from . import launch as launch_mod

router = APIRouter()


@router.get("/super-launcher")
def page(request: Request, db: Session = Depends(get_db)):
    accounts = (db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
                .order_by(models.AdAccount.advertiser_name).all())
    presets = db.query(models.Template).order_by(models.Template.name).all()
    sparks = (db.query(models.SparkCode).filter_by(status="active")
              .order_by(models.SparkCode.name).all())
    # preset id -> destination label, for the auto-lock UI
    dest_labels = {}
    for p in presets:
        fields = launch_mod.synthesize(p)
        dest_labels[p.id] = launch_mod.destination_label(fields)
    return render(request, "super_launcher.html", {
        "accounts": accounts, "presets": presets, "sparks": sparks,
        "dest_labels_json": json.dumps(dest_labels),
        "title": "Super Launcher",
    })


@router.post("/super-launcher/launch")
async def launch(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    template_id = form.get("template_id")
    advertiser_ids = form.getlist("advertiser_ids")
    spark_code_id = form.get("spark_code_id", "")
    if not template_id or not advertiser_ids:
        return RedirectResponse("/super-launcher?err=pick", status_code=303)
    template = db.get(models.Template, int(template_id))
    if not template:
        return RedirectResponse("/super-launcher?err=preset", status_code=303)
    accounts = (db.query(models.AdAccount)
                .filter(models.AdAccount.advertiser_id.in_(advertiser_ids)).all())
    overrides = {}
    if spark_code_id:
        overrides["spark_code_id"] = int(spark_code_id)
    fields = launch_mod.synthesize(template, overrides)
    batch_ref = engine.run_batch(db, accounts, fields)
    return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)
