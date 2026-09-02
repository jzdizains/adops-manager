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

from .. import models, pnl_data, queries, spark_web_api, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/")
def overview(request: Request, db: Session = Depends(get_db)):
    """Home = the live command center (Overview + the old Live page merged).
    Today's KPIs are server-rendered, then the page polls /performance/data
    every 15s (overlap-guarded) and streams the live event feed."""
    start_utc, end_utc = timeutil.range_bounds("today")
    kpis = pnl_data.overall_totals(db, start_utc, end_utc)

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

    # ---- "Needs attention" = the unified inbox (single source of truth) -----
    from .. import inbox as inbox_mod
    inbox_items = inbox_mod.build(db)
    inbox_counts = inbox_mod.counts(inbox_items)

    # chart series: last 14 days of revenue / spend / profit (client redraws)
    days, rev_series, spend_series, profit_series = [], [], [], []
    for offset in range(-13, 1):
        d_start = timeutil.local_midnight_utc(offset)
        d_end = timeutil.local_midnight_utc(offset + 1)
        t = pnl_data.overall_totals(db, d_start, d_end)
        days.append(timeutil.local_date_str(d_start + (d_end - d_start) / 2))
        rev_series.append(round(t["revenue"], 2))
        spend_series.append(round(t["spend"], 2))
        profit_series.append(round(t["profit"], 2))

    return render(request, "home.html", {
        "title": "Home",
        "kpis": kpis,
        "top_accounts": [{"advertiser_id": r.advertiser_id,
                          "name": names.get(r.advertiser_id, r.advertiser_id),
                          "spend": float(r.spend or 0), "campaigns": r.campaigns}
                         for r in top_accounts],
        "active_campaigns": active,
        "account_names": names,
        "synced_ago": queries.campaigns_synced_ago(db),
        "attention": inbox_items[:6], "inbox_counts": inbox_counts,
        "chart_json": json.dumps({"labels": days, "series": {
            "revenue": rev_series, "spend": spend_series, "profit": profit_series}}),
    })


@router.get("/accounts")
def accounts_page(request: Request, db: Session = Depends(get_db)):
    show_lost = request.query_params.get("show_lost") == "1"
    all_accounts = db.query(models.AdAccount).order_by(models.AdAccount.advertiser_name).all()
    lost = [a for a in all_accounts if a.status == "ACCESS_LOST"]
    accounts = all_accounts if show_lost else [a for a in all_accounts
                                               if a.status != "ACCESS_LOST"]
    # per-account facts: BC, campaigns (active/total), spend today, last launch, state
    from sqlalchemy import case, func
    from .. import rules as rules_mod
    bcs = {b.bc_id: b.name for b in db.query(models.BusinessCenter).all()}
    camp = {}
    for aid, total, active, spend in (db.query(models.CampaignRecord.advertiser_id,
                                               func.count(models.CampaignRecord.id),
                                               func.sum(case((models.CampaignRecord.operation_status == "ENABLE", 1), else_=0)),
                                               func.sum(models.CampaignRecord.spend_today))
                                      .group_by(models.CampaignRecord.advertiser_id)):
        camp[aid] = (int(total or 0), int(active or 0), float(spend or 0))
    last_launch = {aid: dt for aid, dt in (db.query(models.LaunchLog.advertiser_id, func.max(models.LaunchLog.created_at))
                                           .filter(models.LaunchLog.ok == True).group_by(models.LaunchLog.advertiser_id))}  # noqa: E712
    facts = {}
    for a in accounts:
        total, active, spend = camp.get(a.advertiser_id, (0, 0, 0.0))
        if a.status and "ENABLE" not in a.status.upper():
            state = "blocked"
        elif rules_mod.in_cooldown(a):
            state = "cooldown"
        elif active:
            state = "active"
        elif total or a.advertiser_id in last_launch:
            state = "used"
        else:
            state = "fresh"
        facts[a.advertiser_id] = {"bc": bcs.get(a.owner_bc_id or "", ""), "total": total, "active": active,
                                  "spend": spend, "last": last_launch.get(a.advertiser_id), "state": state}
    counts = {k: sum(1 for f in facts.values() if f["state"] == k) for k in ("fresh", "used", "active", "cooldown", "blocked")}
    return render(request, "accounts.html", {
        "accounts": accounts, "title": "Ad Accounts", "facts": facts, "counts": counts,
        "lost_count": len(lost), "show_lost": show_lost,
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
