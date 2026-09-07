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
    pages = db.query(models.InstantPage).order_by(models.InstantPage.name, models.InstantPage.owner_advertiser_id).all()
    accounts = queries.enabled_accounts(db)
    names = {a.advertiser_id: a.advertiser_name for a in accounts}
    # the same name across accounts = one preset-usable page; count copies per name
    copies: dict[str, int] = {}
    for p in pages:
        copies[p.name] = copies.get(p.name, 0) + 1
    return render(request, "instant_pages.html", {
        "pages": pages, "accounts": accounts, "names": names, "title": "Instant Pages",
        "copies": copies, "status_labels": STATUS_LABELS,
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


@router.post("/instant-pages/clone")
def clone(page_id: str = Form(...), from_advertiser_id: str = Form(...),
          to_advertiser_id: str = Form(...), db: Session = Depends(get_db)):
    if not spark_web_api.load_cookies():
        return RedirectResponse("/instant-pages?err=" + quote(
            "Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    try:
        spark_web_api.clone_instant_page(page_id, from_advertiser_id, to_advertiser_id)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse("/instant-pages?err=" + quote(f"Clone failed: {str(e)[:200]}"), status_code=303)
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
