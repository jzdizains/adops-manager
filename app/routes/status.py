"""Status monitor — all campaigns across all accounts with status/spend,
pause/resume, and a manual sync ('synced X ago')."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import live_spend, models, queries
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/status")
def status_page(request: Request, db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip().lower()
    records = (db.query(models.CampaignRecord)
               .order_by(models.CampaignRecord.spend_today.desc(),
                         models.CampaignRecord.campaign_name).all())
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    rows = []
    for r in records:
        acct = accounts.get(r.advertiser_id)
        name = (acct.advertiser_name if acct else r.advertiser_id) or r.advertiser_id
        if q and q not in r.campaign_name.lower() and q not in name.lower():
            continue
        rows.append({"r": r, "account_name": name})
    total_spend = sum(r["r"].spend_today for r in rows)
    active = sum(1 for r in rows if r["r"].operation_status == "ENABLE")
    return render(request, "status.html", {
        "rows": rows, "total_spend": total_spend, "active_count": active,
        "synced_ago": queries.campaigns_synced_ago(db), "q": q,
        "title": "Status",
    })


@router.post("/status/sync")
def sync_now(db: Session = Depends(get_db)):
    live_spend.sync_campaigns(db)
    return RedirectResponse("/status", status_code=303)
