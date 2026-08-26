"""TikTok instant page assets — synced per owner account; cloning to other
accounts rides the cookie web path (no Marketing-API endpoint)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, queries, spark_web_api, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/instant-pages")
def page(request: Request, db: Session = Depends(get_db)):
    pages = db.query(models.InstantPage).order_by(models.InstantPage.name).all()
    accounts = queries.enabled_accounts(db)
    names = {a.advertiser_id: a.advertiser_name for a in accounts}
    return render(request, "instant_pages.html", {
        "pages": pages, "accounts": accounts, "names": names, "title": "Instant Pages",
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/instant-pages/sync")
def sync(db: Session = Depends(get_db)):
    synced = 0
    for acct in queries.enabled_accounts(db):
        try:
            data = tiktok_api.list_instant_pages(acct.access_token, acct.advertiser_id)
        except tiktok_api.TikTokError:
            continue
        for p in data.get("list", []):
            pid = str(p.get("page_id", ""))
            if not pid:
                continue
            row = (db.query(models.InstantPage)
                   .filter_by(page_id=pid, owner_advertiser_id=acct.advertiser_id).first())
            if not row:
                row = models.InstantPage(page_id=pid, owner_advertiser_id=acct.advertiser_id)
                db.add(row)
            row.name = p.get("title", p.get("name", "")) or row.name
            row.status = str(p.get("status", "")) or row.status
            row.preview_url = p.get("preview_url", "") or row.preview_url
            synced += 1
    db.commit()
    return RedirectResponse(f"/instant-pages?ok=synced+{synced}", status_code=303)


@router.post("/instant-pages/clone")
def clone(page_id: str = Form(...), from_advertiser_id: str = Form(...),
          to_advertiser_id: str = Form(...), db: Session = Depends(get_db)):
    try:
        spark_web_api.clone_instant_page(page_id, from_advertiser_id, to_advertiser_id)
        return RedirectResponse("/instant-pages?ok=cloned", status_code=303)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse(f"/instant-pages?err={str(e)[:200]}", status_code=303)
