"""Super Launcher — the marquee feature. Pick accounts (checkboxes) + a preset;
the destination auto-locks from the preset; one click launches to all selected
accounts via the launch engine."""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
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
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)                 # the view's accounts, presets, sparks, creatives
    accounts = [a for a in (db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
                            .order_by(models.AdAccount.advertiser_name).all()) if sc.allows(a.advertiser_id)]
    presets = sc.owned(db.query(models.Template), models.Template).order_by(models.Template.name).all()
    sparks = (sc.owned(db.query(models.SparkCode), models.SparkCode).filter_by(status="active")
              .order_by(models.SparkCode.name).all())
    creatives_available = (sc.owned(db.query(models.Creative), models.Creative)
                           .filter_by(status="available", kind="video").count())
    carousels_available = (sc.owned(db.query(models.Creative), models.Creative)
                           .filter_by(status="available", kind="carousel").count())
    # preset id -> destination label, for the auto-lock UI
    dest_labels = {}
    for p in presets:
        fields = launch_mod.synthesize(p)
        dest_labels[p.id] = launch_mod.destination_label(fields)
    picker = account_picker_context(db, accounts)
    preset_info = preset_facts(presets)
    bcs_by_id = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    bc_counts: dict[str, int] = {}
    for a in accounts:
        if a.owner_bc_id:
            bc_counts[a.owner_bc_id] = bc_counts.get(a.owner_bc_id, 0) + 1
    bcs = sorted(({"id": bid, "name": (bcs_by_id[bid].name if bid in bcs_by_id else bid), "accounts": n}
                  for bid, n in bc_counts.items()), key=lambda b: b["name"].lower())
    return render(request, "super_launcher.html", {
        "accounts": accounts, "presets": presets, "sparks": sparks,
        **picker, "preset_info_json": json.dumps(preset_info), "bcs_json": json.dumps(bcs),
        "creatives_available": creatives_available, "carousels_available": carousels_available,
        "dest_labels_json": json.dumps(dest_labels),
        "title": "Super Launcher",
    })


@router.post("/super-launcher/refresh-accounts")
def refresh_accounts(request: Request, db: Session = Depends(get_db)):
    """↻ Refresh on the account picker: re-read every enabled account's status from
    TikTok (/advertiser/info/, 100 ids per call, grouped by token), queue a campaign
    sync for the campaign-level blocks, and answer the picker's per-account state so
    the list updates in place. Nothing is written to TikTok."""
    from .. import jobs, scope as scope_mod, tiktok_api
    sc = scope_mod.for_request(request, db)
    accounts = [a for a in (db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
                            .order_by(models.AdAccount.advertiser_name).all()) if sc.allows(a.advertiser_id)]
    before = {a.advertiser_id: block_reason(a, None, {}) for a in accounts}
    by_token: dict[str, list] = {}
    for a in accounts:
        if a.access_token:
            by_token.setdefault(a.access_token, []).append(a.advertiser_id)
    synced, errors = 0, []
    info_by_id: dict[str, dict] = {}
    for tok, ids in by_token.items():
        for i in range(0, len(ids), 100):
            try:
                for info in tiktok_api.get_advertiser_info(tok, ids[i:i + 100]):
                    info_by_id[str(info.get("advertiser_id", ""))] = info
            except tiktok_api.TikTokError as e:
                errors.append(f"{e.code}")
    for a in accounts:
        st = str((info_by_id.get(a.advertiser_id) or {}).get("status") or "")
        if st:
            a.status = st
            synced += 1
    db.commit()
    queued = False
    if not db.query(models.Job).filter(models.Job.kind == "status_sync", models.Job.status.in_(("queued", "claimed", "running"))).count():
        jobs.enqueue(db, "status_sync", "Sync campaigns from TikTok", {}, href="/status")
        queued = True
    picker = account_picker_context(db, accounts)
    changed = sum(1 for a in accounts if block_reason(a, None, {}) != before[a.advertiser_id])
    return JSONResponse({"ok": True, "synced": synced, "changed": changed, "queued": queued,
                         "errors": errors[:3], "info": picker["info"], "counts": picker["counts"]})


@router.get("/super-launcher/profile-videos.json")
def profile_videos_json(request: Request, db: Session = Depends(get_db)):
    """Every post of every profile the Business Center shares — the picker's data.
    ?bc_id=… (&refresh=1 to re-read TikTok instead of the 10-minute cache)."""
    from .. import profile_videos, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    bc_id = (request.query_params.get("bc_id") or "").strip()
    if not bc_id:
        return JSONResponse({"ok": False, "error": "Pick a Business Center.", "profiles": []})
    if not any(a.owner_bc_id == bc_id and sc.allows(a.advertiser_id)
               for a in db.query(models.AdAccount).filter(models.AdAccount.owner_bc_id == bc_id)):
        return JSONResponse({"ok": False, "error": "That Business Center has no account in this view.", "profiles": []})
    return JSONResponse(profile_videos.list_for_bc(db, sc, bc_id, refresh=request.query_params.get("refresh") == "1"))


# ---------------------------------------------------------------------------
# the creative board (v123): one list of picks from every source
# ---------------------------------------------------------------------------

ITEMS_MAX = 200


def parse_items(raw) -> list[dict]:
    """The board's hidden field: a JSON list of {kind: library|spark|profile, …}.
    Anything unreadable is dropped; at most ITEMS_MAX picks."""
    try:
        items = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "")
        if kind in ("library", "spark") and str(it.get("id") or "").isdigit():
            out.append({"kind": kind, "id": int(it["id"])})
        elif kind == "profile" and str(it.get("item_id") or "").isdigit():
            out.append({**it, "kind": "profile"})
    return out[:ITEMS_MAX]


def creative_units(db: Session, sc, raw) -> tuple[list[tuple[str, int]], str]:
    """The board's picks → [("library", creative_id) | ("spark", spark_code_id)] in the
    picked order, only what this workspace owns (a profile post becomes its spark row).
    Returns (units, problem)."""
    from .. import profile_videos
    items = parse_items(raw)
    if not items:
        return [], "Pick at least one creative."
    lib_ids = [it["id"] for it in items if it["kind"] == "library"]
    spark_ids = [it["id"] for it in items if it["kind"] == "spark"]
    profile_items = [it for it in items if it["kind"] == "profile"]
    lib = {c.id: c for c in sc.owned(db.query(models.Creative), models.Creative).filter(models.Creative.id.in_(lib_ids))} if lib_ids else {}
    sparks = {s.id: s for s in sc.owned(db.query(models.SparkCode), models.SparkCode).filter(models.SparkCode.id.in_(spark_ids))} if spark_ids else {}
    by_item = {r.tiktok_item_id: r for r in profile_videos.ensure_spark_rows(db, sc, profile_items)} if profile_items else {}
    units: list[tuple[str, int]] = []
    seen: set = set()
    for it in items:
        if it["kind"] == "library" and it["id"] in lib:
            c = lib[it["id"]]
            if c.status == "processing" or (c.error or ""):
                continue                                              # still rendering / broken: never launched
            unit = ("library", c.id)
        elif it["kind"] == "spark" and it["id"] in sparks:
            unit = ("spark", it["id"])
        elif it["kind"] == "profile" and str(it["item_id"]) in by_item:
            unit = ("spark", by_item[str(it["item_id"])].id)
        else:
            continue
        if unit not in seen:
            seen.add(unit)
            units.append(unit)
    if not units:
        return [], "None of the picked creatives is in this workspace (or they are still processing)."
    return units, ""


# ---------------------------------------------------------------------------
# autosave (v123): the launcher's state per user, so closing the tab loses nothing
# ---------------------------------------------------------------------------

DRAFT_MAX = 96 * 1024


def draft_key(sc) -> str:
    return f"launch_draft:u{sc.me_id if sc.me_id is not None else 0}"


def clear_draft(db: Session, sc) -> None:
    from .. import queries
    try:
        queries.set_setting(db, draft_key(sc), "")
    except Exception:  # noqa: BLE001 — a draft is a convenience, never a reason to fail a launch
        pass


@router.get("/super-launcher/draft.json")
def draft_get(request: Request, db: Session = Depends(get_db)):
    from .. import queries, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    raw = queries.get_setting(db, draft_key(sc), "")
    try:
        state = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        state = None
    return JSONResponse({"ok": True, "draft": state if isinstance(state, dict) else None})


@router.post("/super-launcher/draft")
async def draft_put(request: Request, db: Session = Depends(get_db)):
    """Save (state=JSON) or discard (clear=1) the viewer's unfinished launch."""
    from .. import queries, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    form = await request.form()
    if form.get("clear"):
        clear_draft(db, sc)
        return JSONResponse({"ok": True, "cleared": True})
    raw = str(form.get("state") or "")
    if len(raw) > DRAFT_MAX:
        return JSONResponse({"ok": False, "error": "draft too large"}, status_code=413)
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "bad state"}, status_code=400)
    if not isinstance(state, dict):
        return JSONResponse({"ok": False, "error": "bad state"}, status_code=400)
    queries.set_setting(db, draft_key(sc), json.dumps(state))
    return JSONResponse({"ok": True})


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


@router.get("/accounts/picker.json")
def accounts_picker_json(request: Request, db: Session = Depends(get_db)):
    """Feed for the reusable account-picker pop-up (UI.pickAccounts): every enabled
    account this user can see, with its Business Center and fresh/used/live/blocked
    state, plus the BC list and state counts. Read-only."""
    from .. import queries, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    accounts = [a for a in queries.enabled_accounts(db) if sc.allows(a.advertiser_id)]
    ctx = account_picker_context(db, accounts)
    info = ctx["info"]
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    out = [{"id": a.advertiser_id, "name": a.advertiser_name or a.advertiser_id,
            "bc": a.owner_bc_id or "", "bc_name": (bcs[a.owner_bc_id].name if a.owner_bc_id in bcs else "No Business Center"),
            "state": info.get(a.advertiser_id, {}).get("state", "fresh")} for a in accounts]
    bc_counts: dict[str, int] = {}
    for a in accounts:
        bc_counts[a.owner_bc_id or ""] = bc_counts.get(a.owner_bc_id or "", 0) + 1
    bclist = sorted(({"id": k, "name": (bcs[k].name if k in bcs else "No Business Center"), "n": v} for k, v in bc_counts.items()),
                    key=lambda b: b["name"].lower())
    return JSONResponse({"accounts": out, "bcs": bclist, "counts": ctx["counts"]})


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


def eligible_accounts(db: Session, policy: str, limit: int, owner_user_id: int | None = None) -> list[models.AdAccount]:
    """Auto-pick: which accounts qualify under the preset's account policy.

    new_only — never had ANY campaign (no CampaignRecord, no successful launch)
    reuse    — no ACTIVE campaign right now
    Both skip disabled accounts and accounts whose status isn't OK.
    `owner_user_id` (v116) keeps it to one user's workspace — a buyer's auto-pick never
    reaches another buyer's fresh accounts.
    """
    aq = db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
    if owner_user_id is not None:
        aq = aq.filter(models.AdAccount.owner_user_id == owner_user_id)
    accounts = aq.order_by(models.AdAccount.advertiser_name).all()
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
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    template = db.get(models.Template, int(template_id))
    if not template or not sc.owns(template):
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
    fields["_launched_by"] = sc.owner_for_new           # whose workspace the launch (and any claimed account) belongs to
    if spark_code_id:
        sp_row = db.get(models.SparkCode, int(spark_code_id))
        if sp_row is None or not sc.owns(sp_row):
            return RedirectResponse("/super-launcher?err=Pick+a+spark+code.", status_code=303)

    use_queue = form.get("use_queue") is not None
    spark_id = int(spark_code_id) if spark_code_id else None
    picked_ids = [int(x) for x in form.getlist("creative_ids") if str(x).isdigit()]
    if creative_mode == "pick" and not picked_ids:
        return RedirectResponse("/super-launcher?err=Pick+at+least+one+creative.", status_code=303)
    if creative_mode == "spark" and not spark_id:
        return RedirectResponse("/super-launcher?err=Pick+a+spark+code.", status_code=303)
    # profile videos: posts picked straight from the Business Center's profiles — each
    # becomes a spark row (by item id) and the posts are spread over the accounts
    profile_sparks: list = []
    if creative_mode == "profile":
        from .. import profile_videos
        try:
            items = json.loads(form.get("profile_items") or "[]")
        except ValueError:
            items = []
        items = [it for it in items if isinstance(it, dict) and str(it.get("item_id") or "").isdigit()][:200]
        if not items:
            return RedirectResponse("/super-launcher?err=Pick+at+least+one+profile+video.", status_code=303)
        profile_sparks = profile_videos.ensure_spark_rows(db, sc, items)
        fields["creative_source"] = "spark"
        fields["ad_text_mode"] = "fixed"
    # the creative board (v123): library videos/carousels, spark codes and profile posts
    # picked together, spread over the accounts in the picked order
    units: list[tuple[str, int]] = []
    if creative_mode == "items":
        units, why = creative_units(db, sc, form.get("items"))
        if why:
            return RedirectResponse("/super-launcher?err=" + quote(why), status_code=303)
        if all(k == "spark" for k, _ in units):
            fields["creative_source"] = "spark"
            fields["ad_text_mode"] = "fixed"
    use_library = (not spark_id) and creative_mode in ("library", "carousel", "pick")
    # creative → account mapping (library only): 1 creative per N accounts
    per_creative = max(_int("accounts_per_creative", 1), 1)
    creatives_count = len(picked_ids) if creative_mode == "pick" else _int("creatives_count")   # 0 = as many as needed
    assign_mode = (use_library and (per_creative > 1 or creatives_count > 0)) or bool(profile_sparks) or bool(units)

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
                                 use_library=use_library, launched_by=sc.owner_for_new)
            clear_draft(db, sc)
            return RedirectResponse("/queue?ok=queued", status_code=303)
        accounts = eligible_accounts(db, fields.get("account_policy", "new_only"), count, owner_user_id=sc.user_id)
        if not accounts:
            return RedirectResponse("/super-launcher?err=noeligible", status_code=303)
    else:
        advertiser_ids = [a for a in form.getlist("advertiser_ids") if sc.allows(a)]   # only the view's accounts
        if not advertiser_ids:
            return RedirectResponse("/super-launcher?err=pick", status_code=303)
        if queue_ok:
            from .. import queue_worker
            queue_worker.enqueue(db, template.id, spark_id, advertiser_ids=advertiser_ids,
                                 use_library=use_library, launched_by=sc.owner_for_new)
            clear_draft(db, sc)
            return RedirectResponse("/queue?ok=queued", status_code=303)
        # preserve the picked order, dedupe
        seen: set = set()
        ordered = [a for a in advertiser_ids if not (a in seen or seen.add(a))]
        by_id = {a.advertiser_id: a for a in db.query(models.AdAccount)
                 .filter(models.AdAccount.advertiser_id.in_(ordered)).all()}
        accounts = [by_id[i] for i in ordered if i in by_id]

    if units:
        # 0 accounts per creative = spread the picks evenly over every selected account
        # (one pick → every account, like the old spark mode); N = each pick covers N
        per = _int("accounts_per_creative", 0)
        if per <= 0:
            per = max(1, -(-len(accounts) // len(units)))
        else:
            accounts = accounts[:len(units) * per]                    # every account gets a creative; none goes without
        assigned = [(a, units[i // per]) for i, a in enumerate(accounts)]
        lib_pairs = [[a.advertiser_id, ref_id] for a, (k, ref_id) in assigned if k == "library"]
        spk_pairs = [[a.advertiser_id, ref_id] for a, (k, ref_id) in assigned if k == "spark"]
        batch_ref = engine.queue_launch(db, f"Launch {template.name} → {len(assigned)} account(s) · {len(units)} creative(s)",
                                        [a.advertiser_id for a, _ in assigned], fields,
                                        pairs=lib_pairs or None, spark_pairs=spk_pairs or None)
        clear_draft(db, sc)
        return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)
    if profile_sparks:
        from .. import profile_videos
        accounts = accounts[:len(profile_sparks) * per_creative]     # every account gets a post; none goes without
        pairs = profile_videos.assign(accounts, profile_sparks, per_creative)
        batch_ref = engine.queue_launch(db, f"Launch {template.name} → {len(pairs)} account(s) · {len(profile_sparks)} profile video(s)",
                                        [a.advertiser_id for a, _ in pairs], fields,
                                        spark_pairs=[[a.advertiser_id, sid] for a, sid in pairs])
    elif assign_mode:
        if creative_mode == "pick":
            by_cid = {c.id: c for c in sc.owned(db.query(models.Creative), models.Creative).filter(models.Creative.id.in_(picked_ids))}
            avail = [by_cid[i] for i in picked_ids if i in by_cid]
        else:
            avail = (sc.owned(db.query(models.Creative), models.Creative)
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
    clear_draft(db, sc)
    return RedirectResponse(f"/campaigns/result/{batch_ref}", status_code=303)
