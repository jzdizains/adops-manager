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


# Business Center statuses (Enumeration – Business Center Status): REVIEWING,
# DENY, ENABLE, PUNISH — plus our own ACCESS_LOST when the login no longer sees it.
BC_OK = ("", "ENABLE")
# campaign secondary statuses that say the ACCOUNT is blocked (the campaign
# sync sees these within minutes, long before the advertiser status catches up)
ACCOUNT_BLOCK_CAMPAIGN_STATUSES = ("ADVERTISER_ACCOUNT_PUNISH", "CAMPAIGN_STATUS_ADVERTISER_ACCOUNT_PUNISH",
                                   "CAMPAIGN_STATUS_ADVERTISER_AUDIT_DENY", "ADVERTISER_CONTRACT_PENDING",
                                   "CAMPAIGN_STATUS_ADVERTISER_CONTRACT_PENDING")


def bc_block(bc) -> str:
    """'' when the Business Center can run ads, else its status in words."""
    st = ((bc.status if bc else "") or "").upper()
    if st in BC_OK:
        return ""
    return {"PUNISH": "punished", "DENY": "rejected", "REVIEWING": "in review",
            "ACCESS_LOST": "access lost"}.get(st, st.lower())


def account_level_blocks(db: Session) -> dict[str, str]:
    """advertiser_id → campaign secondary status that means the account itself
    is punished / denied, from the campaign cache."""
    out: dict[str, str] = {}
    for adv, sec in (db.query(models.CampaignRecord.advertiser_id, models.CampaignRecord.secondary_status)
                     .filter(models.CampaignRecord.secondary_status.in_(ACCOUNT_BLOCK_CAMPAIGN_STATUSES)).distinct()):
        out[adv] = sec
    return out


def block_reason(a, bc, punished: dict[str, str]) -> str:
    """Why this account cannot be launched to right now — '' when it can."""
    if a.status and "ENABLE" not in a.status.upper():
        return "account " + a.status.replace("STATUS_", "").replace("_", " ").lower()
    b = bc_block(bc)
    if b:
        return f"Business Center {b}"
    sec = punished.get(a.advertiser_id)
    if sec:
        return sec.replace("CAMPAIGN_STATUS_", "").replace("_", " ").lower()
    return ""


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
    picker = account_picker_context(db, accounts)
    preset_info = preset_facts(presets)
    return render(request, "super_launcher.html", {
        "accounts": accounts, "presets": presets, "sparks": sparks,
        **picker, "preset_info_json": json.dumps(preset_info),
        "creatives_available": creatives_available, "carousels_available": carousels_available,
        "dest_labels_json": json.dumps(dest_labels),
        "title": "Super Launcher",
    })


def account_picker_context(db: Session, accounts: list) -> dict:
    """What the account picker (shared by the Super Launcher and Create
    Campaign) needs per account: BC grouping, fresh/used/live/cooling/blocked
    state with the block reason, per-BC status, and state counts."""
    from .. import rules as rules_mod
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    with_campaigns = {r[0] for r in db.query(models.CampaignRecord.advertiser_id).distinct()}
    with_active = {r[0] for r in (db.query(models.CampaignRecord.advertiser_id)
                                  .filter(models.CampaignRecord.operation_status == "ENABLE").distinct())}
    ever_launched = {r[0] for r in (db.query(models.LaunchLog.advertiser_id)
                                    .filter(models.LaunchLog.ok == True).distinct())}  # noqa: E712
    punished = account_level_blocks(db)
    info = {}
    groups: dict[str, list] = {}
    bc_status: dict[str, str] = {}
    for a in accounts:
        aid = a.advertiser_id
        reason = block_reason(a, bcs.get(a.owner_bc_id or ""), punished)
        cool = rules_mod.in_cooldown(a)
        if reason:
            state = "blocked"
        elif cool:
            state = "cooldown"
        elif aid in with_active:
            state = "active"
        elif aid in with_campaigns or aid in ever_launched:
            state = "used"
        else:
            state = "fresh"
        info[aid] = {"state": state, "balance": getattr(a, "balance", None), "bc": a.owner_bc_id or "",
                     "reason": reason}
        bc = bcs.get(a.owner_bc_id or "")
        bc_name = bc.name if bc else "No Business Center"
        groups.setdefault(bc_name, []).append(a)
        if bc is not None:
            bc_status[bc_name] = bc_block(bc)
    counts = {k: sum(1 for v in info.values() if v["state"] == k) for k in ("fresh", "used", "active", "cooldown", "blocked")}
    return {"groups": groups, "info": info, "counts": counts, "bc_status": bc_status}


def preset_facts(presets) -> dict:
    """Plain-English facts per preset for the launchers' preview panel."""
    out = {}
    from ..database import SessionLocal
    pinned_names: dict[int, str] = {}
    ids = {int(launch_mod.synthesize(p).get("creative_id") or 0) for p in presets}
    ids.discard(0)
    if ids:
        db = SessionLocal()
        try:
            pinned_names = {c.id: c.name for c in db.query(models.Creative).filter(models.Creative.id.in_(ids)).all()}
        finally:
            db.close()
    for p in presets:
        f = launch_mod.synthesize(p)
        pinned = pinned_names.get(int(f.get("creative_id") or 0), "")
        out[p.id] = {
            "pinned": pinned,
            "objective": dict(launch_mod.OBJECTIVE_OPTIONS).get(p.objective_type, p.objective_type),
            "destination": launch_mod.destination_label(f), "budget": f.get("adgroup_budget") or "",
            "budget_mode": p.campaign_budget_mode or "ABO", "campaign_budget": p.campaign_budget or 0,
            "landing": f.get("landing_page_url") or "", "creative": f.get("creative_source") or "spark",
            "cta": f.get("call_to_action") or "", "duplicates": int(f.get("duplicates") or 1),
            "caps": len(f.get("cost_cap_ladder") or []),
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
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    punished = account_level_blocks(db)
    picked = []
    for a in accounts:
        if block_reason(a, bcs.get(a.owner_bc_id or ""), punished):
            continue  # suspended account / punished BC / account-level campaign block: never auto-picked
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
    picked_ids = [int(x) for x in form.getlist("creative_ids") if str(x).isdigit()]
    if creative_mode == "pick" and not picked_ids:
        return RedirectResponse("/super-launcher?err=Pick+at+least+one+creative.", status_code=303)
    if creative_mode == "spark" and not spark_id:
        return RedirectResponse("/super-launcher?err=Pick+a+spark+code.", status_code=303)
    use_library = (not spark_id) and creative_mode in ("library", "carousel", "pick")
    # creative → account mapping (library only): 1 creative per N accounts
    per_creative = max(_int("accounts_per_creative", 1), 1)
    creatives_count = len(picked_ids) if creative_mode == "pick" else _int("creatives_count")   # 0 = as many as needed
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
        if creative_mode == "pick":
            by_cid = {c.id: c for c in db.query(models.Creative).filter(models.Creative.id.in_(picked_ids))}
            avail = [by_cid[i] for i in picked_ids if i in by_cid]
        else:
            avail = (db.query(models.Creative)
                     .filter_by(status="available", kind=("carousel" if creative_mode == "carousel" else "video"), archived=False)
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
        batch_ref = engine.queue_launch(db, f"Launch {template.name} → {len(pairs)} account(s)",
                                        [a.advertiser_id for a, _ in pairs], fields,
                                        pairs=[[a.advertiser_id, cid] for a, cid in pairs])
    else:
        batch_ref = engine.queue_launch(db, f"Launch {template.name} → {len(accounts)} account(s)",
                                        [a.advertiser_id for a in accounts], fields)
    return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)
