"""Lead-gen form sync/link — per-account assets, synced via the API with a
cookie-web fallback for reads the API doesn't cover."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, queries, spark_web_api, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/lead-forms")
def page(request: Request, db: Session = Depends(get_db)):
    forms = db.query(models.LeadForm).order_by(models.LeadForm.name).all()
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount).all()}
    return render(request, "lead_forms.html", {
        "forms": forms, "names": names, "title": "Lead Forms",
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/lead-forms/sync")
def sync(db: Session = Depends(get_db)):
    synced = 0
    for acct in queries.enabled_accounts(db):
        items = []
        try:
            items = tiktok_api.list_lead_forms(acct.access_token, acct.advertiser_id).get("list", [])
        except tiktok_api.TikTokError:
            try:  # web fallback (§5)
                items = spark_web_api.web_list_lead_forms(acct.advertiser_id).get("data", {}).get("list", [])
            except spark_web_api.WebAuthError:
                continue
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
            synced += 1
    db.commit()
    return RedirectResponse(f"/lead-forms?ok=synced+{synced}", status_code=303)
