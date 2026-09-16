"""TikTok Instant Pages (custom pages) — synced per ad account.

A page belongs to ONE ad account (/page/get/ takes an advertiser_id; the
Marketing API has no share/copy call). The app copes by NAME: a preset stores
the page's name and each launch resolves that account's own page with the
same name. The clone button rides the cookie web path (ads.tiktok.com), which
TikTok doesn't document — it needs the TikTok Cookies page set up and can
stop working without notice.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, queries, spark_web_api, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()

STATUS_LABELS = {"PUBLISHED": "Published", "EDITED": "Draft"}


def sync_account(db: Session, acct: models.AdAccount) -> int:
    """Refresh one account's rows from TikTok; drops rows TikTok no longer
    lists. Returns how many pages the account has. Raises TikTokError."""
    items = tiktok_api.list_all_instant_pages(acct.access_token, acct.advertiser_id)
    seen = set()
    for p in items:
        pid = str(p.get("page_id") or "")
        if not pid:
            continue
        seen.add(pid)
        row = (db.query(models.InstantPage)
               .filter_by(page_id=pid, owner_advertiser_id=acct.advertiser_id).first())
        if not row:
            row = models.InstantPage(page_id=pid, owner_advertiser_id=acct.advertiser_id)
            db.add(row)
        row.name = p.get("title") or row.name or ""
        row.status = str(p.get("status") or "")
        row.preview_url = p.get("preview_url") or ""
    gone = (db.query(models.InstantPage)
            .filter(models.InstantPage.owner_advertiser_id == acct.advertiser_id).all())
    for row in gone:
        if row.page_id not in seen:
            db.delete(row)
    return len(seen)


@router.get("/instant-pages")
def page(request: Request, db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    pages = [p for p in db.query(models.InstantPage).order_by(models.InstantPage.name, models.InstantPage.owner_advertiser_id).all() if sc.allows(p.owner_advertiser_id)]
    accounts = [a for a in queries.enabled_accounts(db) if sc.allows(a.advertiser_id)]
    names = {a.advertiser_id: a.advertiser_name for a in accounts}
    # the same name across accounts = one preset-usable page; count copies per name
    copies: dict[str, int] = {}
    have: dict[str, set] = {}                      # page name → accounts that already hold it
    for p in pages:
        copies[p.name] = copies.get(p.name, 0) + 1
        have.setdefault(p.name, set()).add(p.owner_advertiser_id)
    # Business Centers (in view) with how many of their accounts still LACK each page name
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    by_bc: dict[str, list] = {}
    for a in accounts:
        if a.owner_bc_id:
            by_bc.setdefault(a.owner_bc_id, []).append(a.advertiser_id)
    bcs = sorted(({"bc_id": k, "name": bc_names.get(k, k), "n": len(v)} for k, v in by_bc.items()), key=lambda b: b["name"].lower())
    missing = {name: {bc: sum(1 for aid in ids if aid not in have.get(name, set())) for bc, ids in by_bc.items()} for name in have}
    return render(request, "instant_pages.html", {
        "pages": pages, "accounts": accounts, "names": names, "title": "Instant Pages",
        "copies": copies, "status_labels": STATUS_LABELS, "bcs": bcs, "missing": missing,
        "web_ready": bool(spark_web_api.load_cookies()),
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/instant-pages/sync")
def sync(db: Session = Depends(get_db)):
    accounts = queries.enabled_accounts(db)
    if not accounts:
        return RedirectResponse("/instant-pages?err=" + quote("No enabled ad accounts — connect TikTok first."), status_code=303)
    total, ok, failed = 0, 0, []
    for acct in accounts:
        try:
            total += sync_account(db, acct)
            db.commit()                      # per account, so one failure can't undo the others
            ok += 1
        except tiktok_api.TikTokError as e:
            db.rollback()
            failed.append(f"{acct.advertiser_name or acct.advertiser_id}: {e}")
    msg = f"Found {total} instant page(s) across {ok} account(s)."
    if failed:
        shown = "; ".join(failed[:3]) + (f"; +{len(failed) - 3} more" if len(failed) > 3 else "")
        msg += f" {len(failed)} account(s) couldn't be read — {shown}"
        if not ok:
            return RedirectResponse("/instant-pages?err=" + quote(msg), status_code=303)
    return RedirectResponse("/instant-pages?ok=" + quote(msg), status_code=303)


@router.post("/instant-pages/clone-bc")
def clone_bc(request: Request, page_id: str = Form(...), from_advertiser_id: str = Form(...), bc_id: str = Form(...),
             db: Session = Depends(get_db)):
    """One button: clone the page to EVERY enabled account of a Business Center (in this
    view) that doesn't already hold a page with the same name. Runs as a job — one web
    call per account, paced — and reports per-account results."""
    from .. import jobs, scope as scope_mod
    if not spark_web_api.load_cookies():
        return RedirectResponse("/instant-pages?err=" + quote(
            "Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    sc = scope_mod.for_request(request, db)
    src = db.query(models.InstantPage).filter_by(page_id=page_id, owner_advertiser_id=from_advertiser_id).first()
    if src is None or not sc.allows(from_advertiser_id):
        return RedirectResponse("/instant-pages?err=" + quote("That page is no longer listed — sync and try again."), status_code=303)
    have = {p.owner_advertiser_id for p in db.query(models.InstantPage).filter_by(name=src.name).all()}
    targets = [a for a in queries.enabled_accounts(db)
               if a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id != from_advertiser_id and a.advertiser_id not in have]
    bc = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
    bc_name = (bc.name if bc else "") or bc_id
    if not targets:
        return RedirectResponse("/instant-pages?ok=" + quote(f"Every enabled account in {bc_name} already has “{src.name}” — nothing to clone."), status_code=303)
    job = jobs.enqueue(db, "instant_page_clone_all", f"Clone “{src.name}” → {len(targets)} account(s) in {bc_name}",
                       {"page_id": page_id, "from_advertiser_id": from_advertiser_id, "name": src.name,
                        "targets": [a.advertiser_id for a in targets]}, href="/instant-pages")
    return RedirectResponse("/instant-pages?ok=" + quote(
        f"Cloning “{src.name}” to {len(targets)} account(s) in {bc_name} in the background — you'll get a notification (job #{job.id})."), status_code=303)


def clone_to_many(db: Session, page_id: str, from_advertiser_id: str, targets: list[str], should_stop=None, on_progress=None) -> dict:
    """The job body: clone to each target, re-read it so the copy shows up. One web call per
    account, a short pause between them; per-account failures never stop the rest."""
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
        try:
            body = spark_web_api.clone_instant_page(page_id, from_advertiser_id, adv)
        except spark_web_api.WebAuthError as e:
            failed.append(f"{label}: {str(e)[:120]}")
            continue
        code = str((body or {}).get("code", 0))
        if code not in ("0", "200", ""):
            failed.append(f"{label}: TikTok code {code} {str((body or {}).get('msg') or (body or {}).get('message') or '')[:100]}")
            continue
        ok.append(adv)
        if acct:
            try:
                sync_account(db, acct)
                db.commit()
            except tiktok_api.TikTokError:
                db.rollback()            # the clone went through; the next Sync will list it
        if i + 1 < len(targets):
            _time.sleep(1.0)
    return {"ok": ok, "failed": failed, "stopped": stopped}


@router.post("/instant-pages/clone")
def clone(page_id: str = Form(...), from_advertiser_id: str = Form(...),
          to_advertiser_id: str = Form(...), db: Session = Depends(get_db)):
    if not spark_web_api.load_cookies():
        return RedirectResponse("/instant-pages?err=" + quote(
            "Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    try:
        body = spark_web_api.clone_instant_page(page_id, from_advertiser_id, to_advertiser_id)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse("/instant-pages?err=" + quote(f"Clone failed: {str(e)[:200]}"), status_code=303)
    code = str((body or {}).get("code", 0))
    if code not in ("0", "200", ""):
        msg = str((body or {}).get("msg") or (body or {}).get("message") or "")[:160]
        r = spark_web_api.session_region()
        return RedirectResponse("/instant-pages?err=" + quote(
            f"TikTok refused the clone (code {code}: {msg}) — session region {r['idc'] or 'unknown'} on {r['host']}; the full answer is on Diagnostics."), status_code=303)
    # re-read the target so the copy shows up (and reports honestly if it didn't)
    target = db.query(models.AdAccount).filter_by(advertiser_id=to_advertiser_id).first()
    note = ""
    if target:
        try:
            sync_account(db, target)
            db.commit()
            copied = (db.query(models.InstantPage)
                      .filter(models.InstantPage.owner_advertiser_id == to_advertiser_id).count())
            note = f" {target.advertiser_name or to_advertiser_id} now lists {copied} page(s)."
        except tiktok_api.TikTokError as e:
            db.rollback()
            note = f" (couldn't re-read the target account: {e})"
    return RedirectResponse("/instant-pages?ok=" + quote("TikTok accepted the clone." + note), status_code=303)
