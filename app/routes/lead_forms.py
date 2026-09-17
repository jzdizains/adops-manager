"""Lead-gen form sync/link — per-account assets, synced via the API with a
cookie-web fallback for reads the API doesn't cover."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, queries, spark_web_api, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


def sync_account(db: Session, acct: models.AdAccount) -> int:
    """Read one account's instant forms (API, web fallback) into LeadForm rows.
    Returns rows touched. Raises nothing — an unreadable account counts 0."""
    items = []
    try:
        items = tiktok_api.list_all_lead_forms(acct.access_token, acct.advertiser_id)
    except tiktok_api.TikTokError:
        try:  # web fallback (§5)
            items = spark_web_api.web_list_lead_forms(acct.advertiser_id).get("data", {}).get("list", [])
        except spark_web_api.WebAuthError:
            return 0
    n = 0
    for f in items:
        fid = str(f.get("page_id", f.get("form_id", "")))
        if not fid:
            continue
        row = (db.query(models.LeadForm)
               .filter_by(form_id=fid, owner_advertiser_id=acct.advertiser_id).first())
        if not row:
            row = models.LeadForm(form_id=fid, owner_advertiser_id=acct.advertiser_id)
            db.add(row)
        row.name = f.get("title", f.get("name", "")) or row.name
        row.status = str(f.get("status", "")) or row.status
        n += 1
    return n


@router.get("/lead-forms")
def page(request: Request, db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    forms = [f for f in db.query(models.LeadForm).order_by(models.LeadForm.name, models.LeadForm.owner_advertiser_id).all() if sc.allows(f.owner_advertiser_id)]
    accounts = [a for a in queries.enabled_accounts(db) if sc.allows(a.advertiser_id)]
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount).all()}
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
    return render(request, "lead_forms.html", {
        "forms": forms, "names": names, "title": "Lead Forms", "accounts": accounts,
        "copies": copies, "bcs": bcs, "missing": missing,
        "web_ready": bool(spark_web_api.load_cookies()),
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/lead-forms/sync")
def sync(db: Session = Depends(get_db)):
    synced = 0
    for acct in queries.enabled_accounts(db):
        synced += sync_account(db, acct)
    db.commit()
    return RedirectResponse(f"/lead-forms?ok=synced+{synced}", status_code=303)


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


def clone_to_many(db: Session, form_id: str, from_advertiser_id: str, name: str, targets: list[str],
                  should_stop=None, on_progress=None) -> dict:
    """The job body: one web call per account, verified by re-reading, a short pause
    between accounts; a failure on one account never stops the rest."""
    import time as _time
    ok, failed, stopped = [], [], False
    accts = {a.advertiser_id: a for a in db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(targets or [""])).all()}
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
        if why and ("200000" in why or "expired" in why.lower() or "log in" in why.lower()):
            failed.append(f"{label}: {why} — stopped here, the remaining accounts were not attempted")
            stopped = True
            break
        (failed.append(f"{label}: {why}") if why else ok.append(adv))
        if i + 1 < len(targets):
            _time.sleep(1.5)
    return {"ok": ok, "failed": failed, "stopped": stopped}
