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

from .. import balances, models, pnl_data, queries, spark_web_api, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/")
def overview(request: Request, db: Session = Depends(get_db)):
    range_key = request.query_params.get("range", "today")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    start_utc, end_utc = timeutil.range_bounds(range_key, start, end)

    # Range-driven KPIs: spend from snapshots, revenue/clicks/conv from postbacks
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

    accounts_count = db.query(func.count(models.AdAccount.id)).scalar() or 0
    token_ok = bool(queries.any_access_token(db))
    bcs = db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()
    low_bcs = [b for b in bcs
               if b.balance is not None and b.balance < balances.bc_threshold(b)]

    # ---- "Needs attention" feed --------------------------------------------
    from .. import rules as rules_mod
    day_start = timeutil.local_midnight_utc(0).replace(tzinfo=None)
    attention = []
    for b in low_bcs:
        attention.append({"level": "err", "text": f"BC “{b.name or b.bc_id}” wallet is low: "
                          f"{b.currency} {(b.balance or 0):.2f}",
                          "href": balances.bc_portal_url(b.bc_id),
                          "label": "Open BC ↗", "external": True})
    failed_q = (db.query(func.count(models.LaunchQueueItem.id))
                .filter(models.LaunchQueueItem.status == "failed").scalar() or 0)
    if failed_q:
        attention.append({"level": "err", "text": f"{failed_q} queued launch(es) failed",
                          "href": "/queue", "label": "Queue"})
    pauses_today = (db.query(func.count(models.RuleAction.id))
                    .filter(models.RuleAction.action == "pause",
                            models.RuleAction.ok == True,  # noqa: E712
                            models.RuleAction.created_at >= day_start).scalar() or 0)
    if pauses_today:
        attention.append({"level": "warn", "text": f"Rules paused {pauses_today} campaign(s) today",
                          "href": "/automation", "label": "Review"})
    cooling = sum(1 for a in db.query(models.AdAccount).all() if rules_mod.in_cooldown(a))
    if cooling:
        attention.append({"level": "warn", "text": f"{cooling} account(s) cooling down after launch failures",
                          "href": "/monitor", "label": "Monitor"})
    open_issues = db.query(func.count(models.Issue.id)).scalar() or 0
    if open_issues:
        attention.append({"level": "err", "text": f"{open_issues} TikTok-side issue(s) found "
                          "(payments, account status, rejected ads…)",
                          "href": "/issues", "label": "Review"})
    if not token_ok:
        attention.append({"level": "err", "text": "TikTok isn't connected — nothing can sync or launch",
                          "href": "/accounts", "label": "Connect"})

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

    return render(request, "dashboard.html", {
        "title": "Overview",
        "range_key": range_key, "start": start or "", "end": end or "",
        "kpis": kpis,
        "top_accounts": [{"advertiser_id": r.advertiser_id,
                          "name": names.get(r.advertiser_id, r.advertiser_id),
                          "spend": float(r.spend or 0), "campaigns": r.campaigns}
                         for r in top_accounts],
        "active_campaigns": active,
        "account_names": names,
        "synced_ago": queries.campaigns_synced_ago(db),
        "accounts_count": accounts_count,
        "token_ok": token_ok,
        "bcs": bcs, "low_bcs": low_bcs, "attention": attention,
        "chart_json": json.dumps({"labels": days, "series": {
            "revenue": rev_series, "spend": spend_series, "profit": profit_series}}),
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
