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
    creatives_available = (db.query(models.Creative)
                           .filter_by(status="available").count())
    # preset id -> destination label, for the auto-lock UI
    dest_labels = {}
    for p in presets:
        fields = launch_mod.synthesize(p)
        dest_labels[p.id] = launch_mod.destination_label(fields)
    return render(request, "super_launcher.html", {
        "accounts": accounts, "presets": presets, "sparks": sparks,
        "creatives_available": creatives_available,
        "dest_labels_json": json.dumps(dest_labels),
        "title": "Super Launcher",
    })


def eligible_accounts(db: Session, policy: str, limit: int) -> list[models.AdAccount]:
    """Auto-pick: which accounts qualify under the preset's account policy.

    new_only — never had ANY campaign (no CampaignRecord, no successful launch)
    reuse    — no ACTIVE campaign right now
    Both skip disabled accounts and accounts whose status isn't OK.
    """
    accounts = (db.query(models.AdAccount)
                .filter(models.AdAccount.enabled == True)  # noqa: E712
                .order_by(models.AdAccount.advertiser_name).all())
    with_campaigns = {r[0] for r in db.query(models.CampaignRecord.advertiser_id).distinct()}
    with_active = {r[0] for r in (db.query(models.CampaignRecord.advertiser_id)
                                  .filter(models.CampaignRecord.operation_status == "ENABLE")
                                  .distinct())}
    ever_launched = {r[0] for r in (db.query(models.LaunchLog.advertiser_id)
                                    .filter(models.LaunchLog.ok == True).distinct())}  # noqa: E712
    from .. import rules as rules_mod
    picked = []
    for a in accounts:
        if a.status and "ENABLE" not in a.status.upper():
            continue  # suspended/errored accounts never auto-picked
        if rules_mod.in_cooldown(a):
            continue  # lifecycle cooldown after repeated launch failures
        if policy == "new_only":
            if a.advertiser_id in with_campaigns or a.advertiser_id in ever_launched:
                continue
        else:  # reuse
            if a.advertiser_id in with_active:
                continue
        picked.append(a)
        if len(picked) >= limit:
            break
    return picked


@router.post("/super-launcher/launch")
async def launch(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    template_id = form.get("template_id")
    spark_code_id = form.get("spark_code_id", "")
    creative_mode = form.get("creative_mode", "")     # "" = preset default | "library"
    mode = form.get("mode", "manual")
    if not template_id:
        return RedirectResponse("/super-launcher?err=pick", status_code=303)
    template = db.get(models.Template, int(template_id))
    if not template:
        return RedirectResponse("/super-launcher?err=preset", status_code=303)

    overrides: dict = {}
    if spark_code_id:
        # a spark pick at launch time wins over a library preset
        overrides["spark_code_id"] = int(spark_code_id)
        overrides["creative_source"] = "spark"
        overrides["ad_text_mode"] = "fixed"           # pool texts are library-only
    elif creative_mode == "library":
        # each account pulls the NEXT unused creative from the Creative library
        overrides["creative_source"] = "library"
    fields = launch_mod.synthesize(template, overrides)

    use_queue = form.get("use_queue") is not None
    spark_id = int(spark_code_id) if spark_code_id else None
    use_library = (not spark_id) and creative_mode == "library"

    if mode == "auto":
        try:
            count = max(int(form.get("auto_count") or 0), 0)
        except ValueError:
            count = 0
        if count < 1:
            return RedirectResponse("/super-launcher?err=pick", status_code=303)
        if use_queue:
            from .. import queue_worker
            queue_worker.enqueue(db, template.id, spark_id, auto_count=count,
                                 use_library=use_library)
            return RedirectResponse("/queue?ok=queued", status_code=303)
        accounts = eligible_accounts(db, fields.get("account_policy", "new_only"), count)
        if not accounts:
            return RedirectResponse("/super-launcher?err=noeligible", status_code=303)
    else:
        advertiser_ids = form.getlist("advertiser_ids")
        if not advertiser_ids:
            return RedirectResponse("/super-launcher?err=pick", status_code=303)
        if use_queue:
            from .. import queue_worker
            queue_worker.enqueue(db, template.id, spark_id, advertiser_ids=advertiser_ids,
                                 use_library=use_library)
            return RedirectResponse("/queue?ok=queued", status_code=303)
        accounts = (db.query(models.AdAccount)
                    .filter(models.AdAccount.advertiser_id.in_(advertiser_ids)).all())

    batch_ref = engine.run_batch(db, accounts, fields)
    return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)
