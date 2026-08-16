"""Home / overview: KPI cards (Spend / Revenue / ROAS / Profit — revenue is
REAL from ConversionSample), metric-comparison chart, Top Ad Accounts, Active
Campaigns with 'synced X ago', System Status probing real cookie health.
Also /accounts (the synced list) and /admin/cookie-check."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from .. import models, queries, spark_web_api, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/")
def overview(request: Request, db: Session = Depends(get_db)):
    range_key = request.query_params.get("range", "today")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    start_utc, end_utc = timeutil.range_bounds(range_key, start, end)

    rev = queries.revenue_between(db, start_utc, end_utc)
    spend = queries.spend_today(db)  # spend cache is per-today; date ranges refine via Performance
    revenue = rev["revenue"]
    roas = (revenue / spend) if spend > 0 else 0.0
    profit = revenue - spend

    top_accounts = (
        db.query(models.CampaignRecord.advertiser_id,
                 func.sum(models.CampaignRecord.spend_today).label("spend"),
                 func.count(models.CampaignRecord.id).label("campaigns"))
        .group_by(models.CampaignRecord.advertiser_id)
        .order_by(func.sum(models.CampaignRecord.spend_today).desc())
        .limit(8).all())
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount).all()}

    active = (db.query(models.CampaignRecord)
              .filter(models.CampaignRecord.operation_status == "ENABLE")
              .order_by(models.CampaignRecord.spend_today.desc()).limit(10).all())

    accounts_count = db.query(func.count(models.AdAccount.id)).scalar() or 0
    token_ok = bool(queries.any_access_token(db))

    # chart series: last 14 days of revenue + spend samples (client redraws)
    days, rev_series, conv_series = [], [], []
    for offset in range(-13, 1):
        d_start = timeutil.local_midnight_utc(offset)
        d_end = timeutil.local_midnight_utc(offset + 1)
        r = queries.revenue_between(db, d_start, d_end)
        days.append(timeutil.local_date_str(d_start + (d_end - d_start) / 2))
        rev_series.append(round(r["revenue"], 2))
        conv_series.append(r["conversions"])

    return render(request, "dashboard.html", {
        "title": "Overview",
        "range_key": range_key, "start": start or "", "end": end or "",
        "kpis": {"spend": spend, "revenue": revenue, "roas": roas, "profit": profit,
                 "conversions": rev["conversions"]},
        "top_accounts": [{"advertiser_id": r.advertiser_id,
                          "name": names.get(r.advertiser_id, r.advertiser_id),
                          "spend": float(r.spend or 0), "campaigns": r.campaigns}
                         for r in top_accounts],
        "active_campaigns": active,
        "account_names": names,
        "synced_ago": queries.campaigns_synced_ago(db),
        "accounts_count": accounts_count,
        "token_ok": token_ok,
        "chart_json": json.dumps({"labels": days, "series": {
            "revenue": rev_series, "conversions": conv_series}}),
    })


@router.get("/accounts")
def accounts_page(request: Request, db: Session = Depends(get_db)):
    accounts = db.query(models.AdAccount).order_by(models.AdAccount.advertiser_name).all()
    return render(request, "accounts.html", {
        "accounts": accounts, "title": "Ad Accounts",
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
        "synced_at": queries.get_setting(db, "accounts_synced_at", ""),
    })


@router.post("/accounts/{advertiser_id}/toggle")
def toggle_account(advertiser_id: str, db: Session = Depends(get_db)):
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct:
        acct.enabled = not acct.enabled
        db.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.get("/admin/cookie-check")
def cookie_check(db: Session = Depends(get_db)):
    """JSON probe of real cookie health — the System Status card calls this."""
    own = db.query(models.AdAccount).filter(models.AdAccount.enabled == True).first()  # noqa: E712
    verdict = spark_web_api.probe_health(own.advertiser_id if own else None)
    verdict["saved_at"] = spark_web_api.cookies_saved_at()
    return verdict
