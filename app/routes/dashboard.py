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
    """Home = the live command center: profit first, today by hour vs
    yesterday, which creatives won / lost today, what needs attention, and
    the Business Centers at a glance. Live tiles poll /performance/data."""
    from datetime import timedelta
    from .. import creative_perf, hourly
    from .. import inbox as inbox_mod
    from . import super_launcher as sl

    start_utc, end_utc = timeutil.range_bounds("today")
    y_start, y_end = timeutil.range_bounds("yesterday")
    kpis = pnl_data.overall_totals(db, start_utc, end_utc)
    ykpis = pnl_data.overall_totals(db, y_start, y_end)
    tt_clicks = int(db.query(func.coalesce(func.sum(models.CampaignRecord.clicks), 0)).scalar() or 0)
    kpis["tt_clicks"] = tt_clicks
    kpis["epc"] = (kpis["revenue"] / tt_clicks) if tt_clicks else 0.0
    y_tt_clicks = 0     # TikTok clicks aren't snapshotted per day; EPC delta uses postback clicks
    kpis["epc_pb"] = (kpis["revenue"] / kpis["clicks"]) if kpis["clicks"] else 0.0
    ykpis["epc_pb"] = (ykpis["revenue"] / ykpis["clicks"]) if ykpis["clicks"] else 0.0
    # pace of the day: profit at this hour yesterday (so +38% means vs the same time)
    today_day, y_day = timeutil.local_date_str(start_utc), timeutil.local_date_str(y_start)
    h_now = timeutil.now_local().hour
    active_ids = [r[0] for r in db.query(models.CampaignRecord.campaign_id).filter(models.CampaignRecord.operation_status == "ENABLE")]
    all_ids = [r[0] for r in db.query(models.CampaignRecord.campaign_id)]
    ht, hy = hourly.series(db, all_ids, today_day), hourly.series(db, all_ids, y_day)
    rt, ry = hourly.revenue_series(db, None, today_day), hourly.revenue_series(db, None, y_day)
    y_same_hour = {"spend": sum(hy["spend"][:h_now + 1]), "revenue": sum(ry[:h_now + 1])}
    y_same_hour["profit"] = y_same_hour["revenue"] - y_same_hour["spend"]
    hourly_json = json.dumps({"today": {"spend": ht["spend"], "revenue": rt, "conversions": ht["conversions"], "clicks": ht["clicks"]},
                              "yesterday": {"spend": hy["spend"], "revenue": ry, "conversions": hy["conversions"], "clicks": hy["clicks"]},
                              "hour_now": h_now, "has_hourly": hourly.days_available(db) > 0})

    # ---- today's creative tests: families rolled up, winners / learning / losing
    perf = creative_perf.rows(db, start_utc, end_utc, today=True)
    fams = creative_perf.families([r for r in perf if r["spend"] > 0 or r["revenue"] > 0])
    def _cls(f):
        if f["spend"] < 5:
            return "learning"
        return "winner" if f["roas"] >= 1.3 else ("losing" if f["roas"] < 0.9 else "learning")
    for f in fams:
        f["cls"] = _cls(f)
        f["accounts"] = len({r["c"].used_advertiser_id for r in f["rows"]})
        f["thumb_id"] = f["best"]["c"].id if f.get("best") else None
        f["clicks"] = sum(r["clicks"] for r in f["rows"])
        f["epc"] = (f["revenue"] / f["clicks"]) if f["clicks"] else 0.0
    tests = {"n": len(fams), "winners": sum(1 for f in fams if f["cls"] == "winner"),
             "losing": sum(1 for f in fams if f["cls"] == "losing"), "learning": sum(1 for f in fams if f["cls"] == "learning"),
             "launched_today": db.query(models.LaunchLog).filter(models.LaunchLog.ok == True, models.LaunchLog.created_at >= start_utc.replace(tzinfo=None)).count()}  # noqa: E712
    winners = [f for f in fams if f["cls"] == "winner"][:5]
    losers = sorted([f for f in fams if f["cls"] == "losing"], key=lambda f: f["profit"])[:5]

    # ---- attention (unified inbox) + Business Centers -------------------------
    inbox_items = inbox_mod.build(db)
    inbox_counts = inbox_mod.counts(inbox_items)
    accounts = db.query(models.AdAccount).all()
    ctx = sl.account_picker_context(db, accounts)
    bcs = db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()
    spend_by_aid = {r[0]: float(r[1] or 0) for r in db.query(models.CampaignRecord.advertiser_id, func.sum(models.CampaignRecord.spend_today)).group_by(models.CampaignRecord.advertiser_id)}
    src_map = pnl_data.campaign_source_map(db)
    pb = pnl_data.revenue_by_source(db, start_utc, end_utc)
    rev_by_aid: dict[str, float] = {}
    for r in db.query(models.CampaignRecord).all():
        src = src_map.get(r.campaign_id, "")
        if src and src in pb:
            rev_by_aid[r.advertiser_id] = rev_by_aid.get(r.advertiser_id, 0.0) + float(pb[src].get("revenue", 0.0))
    bc_rows = []
    for b in bcs:
        aids = [a.advertiser_id for a in accounts if a.owner_bc_id == b.bc_id]
        st = [ctx["info"][a]["state"] for a in aids if a in ctx["info"]]
        sp = sum(spend_by_aid.get(a, 0.0) for a in aids); rv = sum(rev_by_aid.get(a, 0.0) for a in aids)
        bc_rows.append({"bc": b, "n": len(aids), "fresh": st.count("fresh"), "live": st.count("active"), "blocked": st.count("blocked"),
                        "spend": sp, "revenue": rv, "profit": rv - sp, "low": (b.balance or 0) < (b.alert_threshold or 0)})
    no_bc = [a.advertiser_id for a in accounts if not a.owner_bc_id]

    return render(request, "home.html", {
        "title": "Home",
        "kpis": kpis, "ykpis": ykpis, "y_same_hour": y_same_hour, "tests": tests, "winners": winners, "losers": losers,
        "hourly_json": hourly_json, "h_now": h_now,
        "synced_ago": queries.campaigns_synced_ago(db),
        "attention": inbox_items[:6], "inbox_counts": inbox_counts,
        "bc_rows": bc_rows, "no_bc": len(no_bc), "acct_counts": ctx["counts"], "n_accounts": len(accounts),
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
