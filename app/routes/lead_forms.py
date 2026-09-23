"""Lead-gen form sync/link — per-account assets, synced via the API with a
cookie-web fallback for reads the API doesn't cover."""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models, queries, spark_web_api, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


def sync_account(db: Session, acct: models.AdAccount) -> int:
    """Read one account's instant forms (API, web fallback) into LeadForm rows.
    Returns rows touched. Raises nothing — an unreadable account counts 0."""
    items = []
    from_api = True
    try:
        items = tiktok_api.list_all_lead_forms(acct.access_token, acct.advertiser_id)
    except tiktok_api.TikTokError:
        from_api = False
        try:  # web fallback (§5)
            items = spark_web_api.web_list_lead_forms(acct.advertiser_id).get("data", {}).get("list", [])
        except spark_web_api.WebAuthError:
            return 0
    n = 0
    seen = set()
    for f in items:
        fid = str(f.get("page_id", f.get("form_id", "")))
        if not fid:
            continue
        seen.add(fid)
        row = (db.query(models.LeadForm)
               .filter_by(form_id=fid, owner_advertiser_id=acct.advertiser_id).first())
        if not row:
            row = models.LeadForm(form_id=fid, owner_advertiser_id=acct.advertiser_id)
            db.add(row)
        row.name = f.get("title", f.get("name", "")) or row.name
        row.status = str(f.get("status", "")) or row.status
        n += 1
    # forms TikTok no longer lists are dropped (a deleted form must not count as "has it") —
    # only on a full API answer, never on the web fallback's guessed shape
    for row in [] if not from_api else db.query(models.LeadForm).filter(models.LeadForm.owner_advertiser_id == acct.advertiser_id).all():
        if row.form_id not in seen:
            db.delete(row)
    return n


@router.get("/lead-forms")
def page(request: Request, db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    forms = [f for f in db.query(models.LeadForm).order_by(models.LeadForm.name, models.LeadForm.owner_advertiser_id).all() if sc.allows(f.owner_advertiser_id)]
    accounts = [a for a in queries.enabled_accounts(db) if sc.allows(a.advertiser_id)]
    _all = db.query(models.AdAccount).all()
    names = {a.advertiser_id: a.advertiser_name for a in _all}
    acct_bc = {a.advertiser_id: (a.owner_bc_id or "") for a in _all}   # each form's owner account → its Business Center (for the BC filter)
    # the same name across accounts = one preset-usable form; who has it, and which BC accounts still lack it
    copies: dict[str, int] = {}
    have: dict[str, set] = {}
    for f in forms:
        copies[f.name] = copies.get(f.name, 0) + 1
        have.setdefault(f.name, set()).add(f.owner_advertiser_id)
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    by_bc: dict[str, list] = {}
    for a in accounts:
        if a.owner_bc_id:
            by_bc.setdefault(a.owner_bc_id, []).append(a.advertiser_id)
    bcs = sorted(({"bc_id": k, "name": bc_names.get(k, k), "n": len(v)} for k, v in by_bc.items()), key=lambda b: b["name"].lower())
    missing = {name: {bc: sum(1 for aid in ids if aid not in have.get(name, set())) for bc, ids in by_bc.items()} for name in have}
    # one row per form NAME (a form copied to 282 accounts is 282 DB rows but ONE line here),
    # each expandable to the accounts that have it — otherwise the table is thousands of rows.
    by_name: dict[str, list] = {}
    for f in forms:
        by_name.setdefault(f.name or f.form_id, []).append(f)
    groups = []
    for gname in sorted(by_name, key=lambda s: s.lower()):
        fl = by_name[gname]
        rep = fl[0]                                            # any copy is a fine clone source
        gbcs = sorted({acct_bc.get(f.owner_advertiser_id, "") for f in fl if acct_bc.get(f.owner_advertiser_id)})
        n_pub = sum(1 for f in fl if "PUBLISH" in (f.status or "").upper())
        accts = sorted(({"name": names.get(f.owner_advertiser_id, f.owner_advertiser_id), "form_id": f.form_id,
                         "advertiser_id": f.owner_advertiser_id, "status": f.status or ""} for f in fl),
                       key=lambda a: a["name"].lower())
        groups.append({"name": gname, "count": len(fl), "rep_form_id": rep.form_id, "rep_owner": rep.owner_advertiser_id,
                       "bcs": gbcs, "n_pub": n_pub, "accounts": accts,
                       "cov": "full" if len(fl) >= len(accounts) else "partial"})
    ftpls = sc.owned(db.query(models.FormTemplate), models.FormTemplate).order_by(models.FormTemplate.name).all()
    acct_ids = {a.advertiser_id for a in accounts}
    ftpl_cov = {t.id: len(have.get(t.name, set()) & acct_ids) for t in ftpls}
    form_names = {f.form_id: f"{f.name} · {names.get(f.owner_advertiser_id, f.owner_advertiser_id)}" for f in forms}
    # which accounts already hold each form (by name) and in what state — for the Clone pop-up (v155.2)
    have_status = {gname: {f.owner_advertiser_id: (f.status or "yes") for f in fl} for gname, fl in by_name.items()}
    return render(request, "lead_forms.html", {
        "have_status": have_status,
        "form_templates": ftpls, "ftpl_cov": ftpl_cov, "form_names": form_names,
        "forms": forms, "groups": groups, "names": names, "title": "Lead Forms", "accounts": accounts,
        "copies": copies, "bcs": bcs, "missing": missing, "acct_bc": acct_bc, "n_accounts": len(accounts),
        "web_ready": bool(spark_web_api.load_cookies()),
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/lead-forms/sync")
def sync(request: Request, db: Session = Depends(get_db)):
    """Re-read the forms of the accounts IN VIEW, as a background job (v151 audit)."""
    from urllib.parse import quote
    from .. import jobs, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    ids = [a.advertiser_id for a in queries.enabled_accounts(db) if sc.allows(a.advertiser_id)]
    if not ids:
        return RedirectResponse("/lead-forms?err=" + quote("No enabled ad accounts — connect TikTok first."), status_code=303)
    jobs.enqueue(db, "asset_sync", f"Sync Lead Forms · {len(ids)} account(s)", {"kind": "form", "advertiser_ids": ids}, href="/lead-forms")
    return RedirectResponse("/lead-forms?ok=" + quote(f"Syncing {len(ids)} account(s) in the background — see Jobs."), status_code=303)


@router.get("/lead-forms/inspect")
def inspect(request: Request, form_id: str, advertiser_id: str, db: Session = Depends(get_db)):
    """READ-ONLY: dump one instant form's definition JSON, read through the page editor's
    web session (the same read the clone flow uses). Writes nothing, creates nothing —
    it exists so a form's field structure can be mapped before a builder edits it."""
    from .. import instant_page_web, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    if not sc.allows(advertiser_id):
        return JSONResponse({"ok": False, "error": "That account isn't in your workspace."}, status_code=403)
    if not spark_web_api.load_cookies():
        return JSONResponse({"ok": False, "error": "Inspecting reads through the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."})
    try:
        info, shape, probes = instant_page_web.read_page(str(form_id), str(advertiser_id), source_owner=str(advertiser_id))
    except spark_web_api.WebAuthError as e:
        return JSONResponse({"ok": False, "error": f"TikTok session expired — re-paste cookies. ({str(e)[:120]})"})
    if not instant_page_web._ok(info):
        return JSONResponse({"ok": False, "error": instant_page_web.explain(info, advertiser_id), "probes": probes})
    page = ((info.get("data") or {}).get("page_info") or {}) if isinstance(info.get("data"), dict) else {}
    raw = page.get("data") or ""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
    except (ValueError, TypeError):
        parsed = None
    title = page.get("title") or page.get("name") or ""
    from .. import lead_form_builder
    fields = lead_form_builder.extract_form_fields(parsed if parsed is not None else raw, title=title)
    return JSONResponse({
        "ok": True, "form_id": str(form_id), "read_shape": shape,
        "business_type": page.get("business_type"),
        "title": title,
        "template_id": page.get("template_id"),
        "fields": fields,
        "definition": parsed if parsed is not None else raw,
    })


@router.post("/lead-forms/build")
def build(request: Request,
          template_form_id: str = Form(...), from_advertiser_id: str = Form(...),
          target_advertiser_id: str = Form(...), name: str = Form(...),
          destination_url: str = Form(""), privacy_url: str = Form(""), company_name: str = Form(""),
          thanks_title: str = Form(""), thanks_description: str = Form(""), cta_title: str = Form(""),
          question_label: str = Form(""), question_options: str = Form(""),
          db: Session = Depends(get_db)):
    """Build a new instant form on ONE account from a template form, with the key fields
    overridden, then re-read the account so it shows up. Uses the page-editor web session
    (no browser); runs like the single clone."""
    from .. import lead_form_builder, scope as scope_mod
    if not spark_web_api.load_cookies():
        return RedirectResponse("/lead-forms?err=" + quote("Building uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    sc = scope_mod.for_request(request, db)
    if not (sc.allows(from_advertiser_id) and sc.allows(target_advertiser_id)):
        return RedirectResponse("/lead-forms?err=" + quote("That template form or account isn't in your workspace."), status_code=303)
    name = (name or "").strip()
    if not name:
        return RedirectResponse("/lead-forms?err=" + quote("Give the new form a name."), status_code=303)
    opts = [o.strip() for o in (question_options or "").replace("\r", "").split("\n") if o.strip()]
    edits = {"destination_url": destination_url.strip(), "privacy_url": privacy_url.strip(),
             "company_name": company_name.strip(), "thanks_title": thanks_title.strip(),
             "thanks_description": thanks_description.strip(), "cta_title": cta_title.strip(),
             "question_label": question_label.strip(), "question_options": opts}
    target = db.query(models.AdAccount).filter_by(advertiser_id=target_advertiser_id).first()
    label = (target.advertiser_name if target else "") or target_advertiser_id
    try:
        res = lead_form_builder.build_form(template_form_id, name, target_advertiser_id, edits, source_owner=from_advertiser_id)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse("/lead-forms?err=" + quote(f"TikTok session expired — re-paste cookies. ({str(e)[:120]})"), status_code=303)
    if not res.get("ok"):
        return RedirectResponse("/lead-forms?err=" + quote(f"Couldn't build “{name}” on {label}: {res.get('error', '')[:200]}"), status_code=303)
    try:
        if target:
            sync_account(db, target)
        db.commit()
    except Exception:  # noqa: BLE001 — the form is built; a failed re-read just means Sync later
        db.rollback()
    return RedirectResponse("/lead-forms?ok=" + quote(f"Built “{name}” on {label} — it'll appear by name in your presets."), status_code=303)


def _clone_one(db: Session, form_id: str, from_advertiser_id: str, acct: models.AdAccount, name: str) -> str:
    """Copy one form to one account through the page editor's web API and VERIFY by
    re-reading the target: '' when a form with that name now exists there, else why not.
    An instant form is a TikTok page (business_type LEAD_GEN — that is how /page/get/
    lists it) built in the same Instant Page editor, so it rides the same recorded
    duplicate flow (instant_page_web) with the source's own business_type."""
    from .. import instant_page_web
    try:
        r = instant_page_web.duplicate(form_id, name, acct.advertiser_id, source_owner=from_advertiser_id)
    except spark_web_api.WebAuthError as e:
        return str(e)[:160]
    if r.get("no_access"):
        return r.get("error", "")          # nothing was created — no re-read, no pause (v155.1)
    answered = "" if r.get("ok") else f"TikTok refused the copy — {r.get('error', '')[:140]}"
    try:
        sync_account(db, acct)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        return answered or "copied, but the account couldn't be re-read — Sync later to confirm"
    exists = db.query(models.LeadForm).filter_by(owner_advertiser_id=acct.advertiser_id, name=name).first() is not None
    if exists:
        return ""
    return answered or "TikTok accepted the copy but no form with this name appeared on the account"


@router.post("/lead-forms/clone")
def clone(request: Request, form_id: str = Form(...), from_advertiser_id: str = Form(...), to_advertiser_id: str = Form(...),
          db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    if not spark_web_api.load_cookies():
        return RedirectResponse("/lead-forms?err=" + quote("Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    sc = scope_mod.for_request(request, db)
    src = db.query(models.LeadForm).filter_by(form_id=form_id, owner_advertiser_id=from_advertiser_id).first()
    target = db.query(models.AdAccount).filter_by(advertiser_id=to_advertiser_id).first()
    if src is None or target is None or not sc.allows(from_advertiser_id) or not sc.allows(to_advertiser_id):
        return RedirectResponse("/lead-forms?err=" + quote("That form or account is no longer listed — sync and try again."), status_code=303)
    why = _clone_one(db, form_id, from_advertiser_id, target, src.name)
    label = target.advertiser_name or to_advertiser_id
    if why:
        return RedirectResponse("/lead-forms?err=" + quote(f"Clone to {label} didn't go through: {why}"), status_code=303)
    return RedirectResponse("/lead-forms?ok=" + quote(f"“{src.name}” is now on {label}."), status_code=303)


@router.post("/lead-forms/clone-bc")
def clone_bc(request: Request, form_id: str = Form(...), from_advertiser_id: str = Form(...), bc_id: str = Form(...),
             db: Session = Depends(get_db)):
    """One button: copy the form to EVERY enabled account of a Business Center (in this
    view) that doesn't already hold a form with the same name — as a background job."""
    from .. import jobs, scope as scope_mod
    if not spark_web_api.load_cookies():
        return RedirectResponse("/lead-forms?err=" + quote("Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    sc = scope_mod.for_request(request, db)
    src = db.query(models.LeadForm).filter_by(form_id=form_id, owner_advertiser_id=from_advertiser_id).first()
    if src is None or not sc.allows(from_advertiser_id):
        return RedirectResponse("/lead-forms?err=" + quote("That form is no longer listed — sync and try again."), status_code=303)
    have = {f.owner_advertiser_id for f in db.query(models.LeadForm).filter_by(name=src.name).all()}
    targets = [a for a in queries.enabled_accounts(db)
               if a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id != from_advertiser_id and a.advertiser_id not in have]
    bc = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
    bc_name = (bc.name if bc else "") or bc_id
    if not targets:
        return RedirectResponse("/lead-forms?ok=" + quote(f"Every enabled account in {bc_name} already has “{src.name}” — nothing to clone."), status_code=303)
    job = jobs.enqueue(db, "lead_form_clone_all", f"Clone form “{src.name}” → {len(targets)} account(s) in {bc_name}",
                       {"form_id": form_id, "from_advertiser_id": from_advertiser_id, "name": src.name,
                        "targets": [a.advertiser_id for a in targets]}, href="/lead-forms")
    return RedirectResponse("/lead-forms?ok=" + quote(
        f"Cloning “{src.name}” to {len(targets)} account(s) in {bc_name} in the background — you'll get a notification (job #{job.id})."), status_code=303)


@router.post("/lead-forms/clone-multi")
def clone_multi(request: Request, form_id: str = Form(...), from_advertiser_id: str = Form(...),
                name: str = Form(...), target_ids: str = Form(""), db: Session = Depends(get_db)):
    """Clone a form onto the accounts picked in the account pop-up (comma-separated ids),
    as one background job — the same verified, one-at-a-time flow as the BC clone."""
    from .. import jobs, scope as scope_mod
    if not spark_web_api.load_cookies():
        return RedirectResponse("/lead-forms?err=" + quote("Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    sc = scope_mod.for_request(request, db)
    if not sc.allows(from_advertiser_id):
        return RedirectResponse("/lead-forms?err=" + quote("That form is no longer listed — sync and try again."), status_code=303)
    ids = [x.strip() for x in (target_ids or "").split(",")
           if x.strip() and sc.allows(x.strip()) and x.strip() != from_advertiser_id]
    have = {f.owner_advertiser_id for f in db.query(models.LeadForm).filter_by(name=name).all()}
    ids = [i for i in dict.fromkeys(ids) if i not in have]     # de-dupe, drop accounts that already have it
    if not ids:
        return RedirectResponse("/lead-forms?ok=" + quote(f"Every selected account already has “{name}” — nothing to clone."), status_code=303)
    job = jobs.enqueue(db, "lead_form_clone_all", f"Clone form “{name}” → {len(ids)} account(s)",
                       {"form_id": form_id, "from_advertiser_id": from_advertiser_id, "name": name, "targets": ids}, href="/lead-forms")
    return RedirectResponse("/lead-forms?ok=" + quote(
        f"Cloning “{name}” to {len(ids)} account(s) in the background — you'll get a notification (job #{job.id})."), status_code=303)


def clone_to_many(db: Session, form_id: str, from_advertiser_id: str, name: str, targets: list[str],
                  should_stop=None, on_progress=None) -> dict:
    """The job body: one web call per account, verified by re-reading, a short pause
    between accounts; a failure on one account never stops the rest."""
    import time as _time
    from .. import instant_page_web
    ok, failed, stopped = [], [], False
    no_access: list = []                     # (label, bc) — reported as ONE line with the fix
    accts = {a.advertiser_id: a for a in db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(targets or [""])).all()}
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    for i, adv in enumerate(targets):
        if should_stop and should_stop():
            stopped = True
            break
        acct = accts.get(adv)
        label = (acct.advertiser_name if acct else "") or adv
        if on_progress:
            on_progress(f"{i + 1} of {len(targets)} — {label}")
        if acct is None:
            failed.append(f"{label}: account no longer listed")
            continue
        if db.query(models.LeadForm).filter_by(owner_advertiser_id=adv, name=name).first() is not None:
            ok.append(adv)                       # a re-run never duplicates
            continue
        why = _clone_one(db, form_id, from_advertiser_id, acct, name)
        if why and instant_page_web.is_no_access(why):
            no_access.append((label, bc_names.get(acct.owner_bc_id or "", "")))
            _time.sleep(0.3)
            continue
        if why and ("200000" in why or "expired" in why.lower() or "log in" in why.lower()):
            failed.append(f"{label}: {why} — stopped here, the remaining accounts were not attempted")
            stopped = True
            break
        (failed.append(f"{label}: {why}") if why else ok.append(adv))
        if i + 1 < len(targets):
            _time.sleep(1.5)
    if no_access:
        failed.insert(0, instant_page_web.no_access_summary([x[0] for x in no_access], [x[1] for x in no_access]))
    return {"ok": ok, "failed": failed, "stopped": stopped, "no_access": len(no_access)}
