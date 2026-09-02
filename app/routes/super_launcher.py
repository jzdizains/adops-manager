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
                           .filter_by(status="available", kind="video").count())
    carousels_available = (db.query(models.Creative)
                           .filter_by(status="available", kind="carousel").count())
    # preset id -> destination label, for the auto-lock UI
    dest_labels = {}
    for p in presets:
        fields = launch_mod.synthesize(p)
        dest_labels[p.id] = launch_mod.destination_label(fields)
    # per-account facts for the picker: BC, fresh/used/active, cooldown, balance
    from .. import rules as rules_mod
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    with_campaigns = {r[0] for r in db.query(models.CampaignRecord.advertiser_id).distinct()}
    with_active = {r[0] for r in (db.query(models.CampaignRecord.advertiser_id)
                                  .filter(models.CampaignRecord.operation_status == "ENABLE").distinct())}
    ever_launched = {r[0] for r in (db.query(models.LaunchLog.advertiser_id)
                                    .filter(models.LaunchLog.ok == True).distinct())}  # noqa: E712
    info = {}
    groups: dict[str, list] = {}
    for a in accounts:
        aid = a.advertiser_id
        bad = bool(a.status and "ENABLE" not in a.status.upper())
        cool = rules_mod.in_cooldown(a)
        if bad:
            state = "blocked"
        elif cool:
            state = "cooldown"
        elif aid in with_active:
            state = "active"
        elif aid in with_campaigns or aid in ever_launched:
            state = "used"
        else:
            state = "fresh"
        info[aid] = {"state": state, "balance": getattr(a, "balance", None), "bc": a.owner_bc_id or ""}
        bc = bcs.get(a.owner_bc_id or "")
        groups.setdefault(bc.name if bc else "No Business Center", []).append(a)
    counts = {k: sum(1 for v in info.values() if v["state"] == k) for k in ("fresh", "used", "active", "cooldown", "blocked")}
    preset_info = preset_facts(presets)
    return render(request, "super_launcher.html", {
        "accounts": accounts, "presets": presets, "sparks": sparks,
        "groups": groups, "info": info, "counts": counts, "preset_info_json": json.dumps(preset_info),
        "creatives_available": creatives_available, "carousels_available": carousels_available,
        "dest_labels_json": json.dumps(dest_labels),
        "title": "Super Launcher",
    })


def preset_facts(presets) -> dict:
    """Plain-English facts per preset for the launchers' preview panel."""
    out = {}
    for p in presets:
        f = launch_mod.synthesize(p)
        out[p.id] = {
            "objective": dict(launch_mod.OBJECTIVE_OPTIONS).get(p.objective_type, p.objective_type),
            "destination": launch_mod.destination_label(f), "budget": f.get("adgroup_budget") or "",
            "budget_mode": p.campaign_budget_mode or "ABO", "campaign_budget": p.campaign_budget or 0,
            "landing": f.get("landing_page_url") or "", "creative": f.get("creative_source") or "spark",
            "cta": f.get("call_to_action") or "", "duplicates": int(f.get("duplicates") or 1),
            "policy": f.get("account_policy") or "",
            "smart_plus": bool(f.get("smart_plus")), "ad_text": (f.get("ad_text") or "")[:80],
        }
    return out


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

    def _int(name, default=0):
        try:
            return int(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    overrides: dict = {}
    if spark_code_id:
        # a spark pick at launch time wins over a library preset
        overrides["spark_code_id"] = int(spark_code_id)
        overrides["creative_source"] = "spark"
        overrides["ad_text_mode"] = "fixed"           # pool texts are library-only
    elif creative_mode in ("library", "carousel"):
        # each account pulls the NEXT unused video / carousel from the Creative library
        overrides["creative_source"] = creative_mode
    # duplication overrides (win over the preset's own settings)
    dup = _int("duplicates")
    if dup > 0:
        overrides["duplicates"] = dup                 # ad groups per campaign
    apg = _int("ads_per_group")
    if apg > 0:
        overrides["ads_per_group"] = apg              # ads per ad group
    fields = launch_mod.synthesize(template, overrides)

    use_queue = form.get("use_queue") is not None
    spark_id = int(spark_code_id) if spark_code_id else None
    use_library = (not spark_id) and creative_mode in ("library", "carousel")
    # creative → account mapping (library only): 1 creative per N accounts
    per_creative = max(_int("accounts_per_creative", 1), 1)
    creatives_count = _int("creatives_count")         # 0 = as many as needed
    assign_mode = use_library and (per_creative > 1 or creatives_count > 0)

    # the creative→account assignment needs a fixed account list up front, so it
    # always runs inline (not via the retry queue)
    queue_ok = use_queue and not assign_mode

    if mode == "auto":
        count = max(_int("auto_count"), 0)
        if count < 1:
            return RedirectResponse("/super-launcher?err=pick", status_code=303)
        if queue_ok:
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
        if queue_ok:
            from .. import queue_worker
            queue_worker.enqueue(db, template.id, spark_id, advertiser_ids=advertiser_ids,
                                 use_library=use_library)
            return RedirectResponse("/queue?ok=queued", status_code=303)
        # preserve the picked order, dedupe
        seen: set = set()
        ordered = [a for a in advertiser_ids if not (a in seen or seen.add(a))]
        by_id = {a.advertiser_id: a for a in db.query(models.AdAccount)
                 .filter(models.AdAccount.advertiser_id.in_(ordered)).all()}
        accounts = [by_id[i] for i in ordered if i in by_id]

    if assign_mode:
        avail = (db.query(models.Creative)
                 .filter_by(status="available", kind=("carousel" if creative_mode == "carousel" else "video"))
                 .order_by(models.Creative.id).all())
        import math
        needed = creatives_count if creatives_count > 0 else math.ceil(len(accounts) / per_creative)
        creatives = avail[:needed]
        if not creatives:
            return RedirectResponse(
                "/super-launcher?err=No+available+creatives+in+the+library+—+upload+"
                "or+create+variations+first.", status_code=303)
        if creatives_count > 0:                 # cap accounts to what the creatives cover
            accounts = accounts[:len(creatives) * per_creative]
        pairs = engine.assign_creatives(accounts, creatives, per_creative)
        batch_ref = engine.run_batch_assigned(db, pairs, fields)
    else:
        batch_ref = engine.run_batch(db, accounts, fields)
    return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)
