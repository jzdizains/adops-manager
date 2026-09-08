"""Home / overview: KPI cards (Spend / Revenue / ROAS / Profit — revenue is
REAL from ConversionSample), metric-comparison chart, Top Ad Accounts, Active
Campaigns with 'synced X ago', System Status probing real cookie health.
Also /accounts (the synced list) and /admin/cookie-check."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import case, func

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
    """Every ad account grouped by Business Center: state (fresh/live/used/
    cooling/blocked), balance, spend today, revenue → profit + ROAS, campaigns,
    last launch. Filtering happens in the browser (accounts.js)."""
    from . import super_launcher as sl
    from .. import activity as activity_mod, balances as bal_mod
    show_lost = request.query_params.get("show_lost") == "1"
    all_accounts = db.query(models.AdAccount).order_by(models.AdAccount.advertiser_name).all()
    lost = [a for a in all_accounts if a.status == "ACCESS_LOST"]
    accounts = all_accounts if show_lost else [a for a in all_accounts if a.status != "ACCESS_LOST"]
    ctx = sl.account_picker_context(db, accounts)
    facts = _account_facts(db, accounts, ctx)
    bcs = db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()
    by_bc: dict[str, list] = {}
    for a in accounts:
        by_bc.setdefault(a.owner_bc_id or "", []).append(a)
    groups = []
    for b in bcs + [None]:
        key = b.bc_id if b else ""
        members = by_bc.get(key, [])
        if not members and b is None:
            continue
        # profit first inside a BC, then fresh accounts, then the rest by name
        members.sort(key=lambda a: (-facts[a.advertiser_id]["profit"], 0 if facts[a.advertiser_id]["state"] == "fresh" else 1, (a.advertiser_name or "").lower()))
        st = [facts[a.advertiser_id]["state"] for a in members]
        sp = sum(facts[a.advertiser_id]["spend"] for a in members); rv = sum(facts[a.advertiser_id]["revenue"] for a in members)
        thr = bal_mod.bc_threshold(b) if b else 0.0
        groups.append({"bc": b, "key": key, "name": b.name if b else "No Business Center", "members": members,
                       "n": len(members), "fresh": st.count("fresh"), "live": st.count("active"), "blocked": st.count("blocked") + st.count("cooldown"),
                       "spend": sp, "revenue": rv, "profit": rv - sp, "low": bool(b) and (b.balance or 0) < thr, "threshold": thr,
                       "block": sl.bc_block(b) if b else "", "portal": bal_mod.bc_portal_url(b.bc_id) if b else ""})
    counts = dict(ctx["counts"]); counts["off"] = sum(1 for a in accounts if not a.enabled)
    counts["low"] = sum(1 for a in accounts if a.balance is not None and a.enabled and (a.balance or 0) < 20)
    notes = activity_mod.notes_for(db, "account", [a.advertiser_id for a in accounts])
    return render(request, "accounts.html", {
        "accounts": accounts, "title": "Ad accounts", "facts": facts, "counts": counts, "groups": groups, "notes": notes,
        "lost_count": len(lost), "show_lost": show_lost, "n_bc": len(bcs),
        "tot": {"spend": sum(f["spend"] for f in facts.values()), "revenue": sum(f["revenue"] for f in facts.values())},
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
        "synced_at": queries.get_setting(db, "accounts_synced_at", ""),
    })


def _account_facts(db: Session, accounts: list, ctx: dict) -> dict:
    """Per-account numbers for the Accounts page and the account drawer."""
    camp: dict[str, tuple] = {}
    for aid, total, active, spend in (db.query(models.CampaignRecord.advertiser_id, func.count(models.CampaignRecord.id),
                                               func.sum(case((models.CampaignRecord.operation_status == "ENABLE", 1), else_=0)),
                                               func.sum(models.CampaignRecord.spend_today))
                                      .group_by(models.CampaignRecord.advertiser_id)):
        camp[aid] = (int(total or 0), int(active or 0), float(spend or 0))
    last_launch = {aid: dt for aid, dt in (db.query(models.LaunchLog.advertiser_id, func.max(models.LaunchLog.created_at))
                                           .filter(models.LaunchLog.ok == True).group_by(models.LaunchLog.advertiser_id))}  # noqa: E712
    src_map = pnl_data.campaign_source_map(db)
    pb = pnl_data.revenue_by_source(db, timeutil.local_midnight_utc(0), timeutil.local_midnight_utc(1))
    rev_by_aid: dict[str, float] = {}
    for cid, aid in db.query(models.CampaignRecord.campaign_id, models.CampaignRecord.advertiser_id):
        src = src_map.get(cid, "")
        if src and src in pb:
            rev_by_aid[aid] = rev_by_aid.get(aid, 0.0) + float(pb[src].get("revenue", 0.0))
    facts = {}
    for a in accounts:
        total, active, spend = camp.get(a.advertiser_id, (0, 0, 0.0))
        info = ctx["info"].get(a.advertiser_id, {"state": "fresh", "reason": ""})
        rv = rev_by_aid.get(a.advertiser_id, 0.0)
        facts[a.advertiser_id] = {"total": total, "active": active, "spend": spend, "revenue": rv, "profit": rv - spend,
                                  "roas": (rv / spend) if spend else 0.0, "last": last_launch.get(a.advertiser_id),
                                  "state": info["state"], "reason": info.get("reason", "")}
    return facts


@router.get("/accounts/bc/{bc_id}/detail")
def bc_detail(bc_id: str, db: Session = Depends(get_db)):
    """One Business Center for the Home drawer: its accounts with state, spend, profit — no page change."""
    from . import super_launcher as sl
    from .. import balances as bal_mod
    b = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
    accounts = [a for a in db.query(models.AdAccount).filter(models.AdAccount.owner_bc_id == bc_id).order_by(models.AdAccount.advertiser_name) if a.status != "ACCESS_LOST"]
    if not b and not accounts:
        return JSONResponse({"error": "No such Business Center."}, status_code=404)
    ctx = sl.account_picker_context(db, accounts)
    facts = _account_facts(db, accounts, ctx)
    rows = [{"id": a.advertiser_id, "name": a.advertiser_name or a.advertiser_id, "enabled": bool(a.enabled), "balance": a.balance,
             **{k: facts[a.advertiser_id][k] for k in ("state", "spend", "revenue", "profit", "active", "total")}} for a in accounts]
    rows.sort(key=lambda r: (-r["profit"], 0 if r["state"] == "fresh" else 1, r["name"].lower()))
    st = [r["state"] for r in rows]
    return {"bc_id": bc_id, "name": (b.name if b else "") or bc_id, "balance": (b.balance if b else None), "currency": (b.currency if b else "") or "$",
            "threshold": bal_mod.bc_threshold(b) if b else 0.0, "portal": bal_mod.bc_portal_url(bc_id) if b else "", "block": sl.bc_block(b) if b else "",
            "n": len(rows), "fresh": st.count("fresh"), "live": st.count("active"), "blocked": st.count("blocked") + st.count("cooldown"),
            "spend": sum(r["spend"] for r in rows), "revenue": sum(r["revenue"] for r in rows), "rows": rows}


@router.get("/accounts/{advertiser_id}/detail")
def account_detail(advertiser_id: str, db: Session = Depends(get_db)):
    """JSON for the account drawer: facts, today's campaigns, BC, note, recent launches."""
    from fastapi.responses import JSONResponse
    from . import super_launcher as sl
    from .. import activity as activity_mod
    a = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    ctx = sl.account_picker_context(db, [a])
    f = _account_facts(db, [a], ctx)[advertiser_id]
    bc = db.query(models.BusinessCenter).filter_by(bc_id=a.owner_bc_id or "").first()
    src_map = pnl_data.campaign_source_map(db)
    pb = pnl_data.revenue_by_source(db, timeutil.local_midnight_utc(0), timeutil.local_midnight_utc(1))
    camps = []
    for r in (db.query(models.CampaignRecord).filter_by(advertiser_id=advertiser_id)
              .order_by(models.CampaignRecord.spend_today.desc()).limit(50)):
        src = src_map.get(r.campaign_id, "")
        rv = float(pb.get(src, {}).get("revenue", 0.0)) if src else 0.0
        sp = float(r.spend_today or 0)
        camps.append({"id": r.campaign_id, "name": r.campaign_name, "status": r.operation_status, "secondary": r.secondary_status or "",
                      "spend": sp, "revenue": rv, "profit": rv - sp, "roas": (rv / sp) if sp else 0.0, "conversions": int(r.conversions or 0)})
    launches = [{"at": _ago(lg.created_at), "ok": bool(lg.ok), "campaign_id": lg.campaign_id or "", "error": (lg.error_message or lg.error_code or "")[:140], "preset": lg.template_name or ""}
                for lg in db.query(models.LaunchLog).filter_by(advertiser_id=advertiser_id).order_by(models.LaunchLog.id.desc()).limit(8)]
    return JSONResponse({
        "id": a.advertiser_id, "name": a.advertiser_name or a.advertiser_id, "status": a.status or "", "enabled": bool(a.enabled),
        "currency": a.currency or "USD", "timezone": a.timezone or "", "balance": a.balance, "token_expires": a.token_expires_at.strftime("%Y-%m-%d") if a.token_expires_at else "",
        "cooldown_until": a.cooldown_until.isoformat() if a.cooldown_until else "", "error_count": int(a.error_count or 0),
        "bc": {"id": bc.bc_id, "name": bc.name, "balance": bc.balance, "status": bc.status} if bc else None,
        "facts": {**f, "last": _ago(f["last"]) if f["last"] else ""}, "campaigns": camps, "launches": launches,
        "note": activity_mod.get_note(db, "account", a.advertiser_id),
        "ads_manager": f"https://ads.tiktok.com/i18n/dashboard?aadvid={a.advertiser_id}",
    })


def _ago(dt) -> str:
    if not dt:
        return ""
    from datetime import datetime as _dt, timezone as _tz
    s = int((_dt.now(_tz.utc).replace(tzinfo=None) - dt).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


@router.post("/accounts/{advertiser_id}/toggle")
def toggle_account(request: Request, advertiser_id: str, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct:
        acct.enabled = not acct.enabled
        db.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": bool(acct), "enabled": bool(acct and acct.enabled)})
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{advertiser_id}/transfer")
async def account_transfer(request: Request, advertiser_id: str, db: Session = Depends(get_db)):
    """Move money from the account's Business Center wallet into the ad account
    (the same /bc/transfer/ call the auto top-up rule uses). JSON in, JSON out."""
    from fastapi.responses import JSONResponse
    from .. import activity as activity_mod, tiktok_api
    form = await request.form()
    try:
        amount = float(str(form.get("amount") or "0").replace("$", "").strip())
    except ValueError:
        amount = 0.0
    if amount <= 0:
        return JSONResponse({"ok": False, "error": "Enter an amount above 0."}, status_code=400)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    bc = db.query(models.BusinessCenter).filter_by(bc_id=acct.owner_bc_id or "").first() if acct else None
    if not acct or not bc:
        return JSONResponse({"ok": False, "error": "This account has no Business Center wallet to draw from."}, status_code=400)
    if (bc.balance or 0) < amount:
        return JSONResponse({"ok": False, "error": f"The {bc.name or bc.bc_id} wallet holds {bc.currency} {float(bc.balance or 0):.2f} — not enough for ${amount:.2f}."}, status_code=400)
    topup = models.TopUp(bc_id=bc.bc_id, advertiser_id=acct.advertiser_id, amount=amount, day=timeutil.local_date_str())
    try:
        tiktok_api.bc_transfer(acct.access_token, bc.bc_id, acct.advertiser_id, amount, "RECHARGE")
    except tiktok_api.TikTokError as e:
        topup.ok = False; topup.detail = f"manual transfer FAILED: code={e.code} {e.message}"
        db.add(topup); db.commit()
        return JSONResponse({"ok": False, "error": f"TikTok refused the transfer: {e.message}"}, status_code=502)
    topup.ok = True; topup.detail = "manual transfer from the Accounts page"
    acct.balance = float(acct.balance or 0) + amount
    bc.balance = float(bc.balance or 0) - amount
    db.add(topup)
    activity_mod.record(db, "account", acct.advertiser_id, "transfer", f"${amount:.2f} from {bc.name or bc.bc_id} wallet", request=request, advertiser_id=acct.advertiser_id, commit=False)
    db.commit()
    return JSONResponse({"ok": True, "balance": acct.balance, "bc_balance": bc.balance})


@router.get("/admin/cookie-check")
def cookie_check(db: Session = Depends(get_db)):
    """JSON probe of real cookie health — the System Status card calls this."""
    own = db.query(models.AdAccount).filter(models.AdAccount.enabled == True).first()  # noqa: E712
    verdict = spark_web_api.probe_health(own.advertiser_id if own else None)
    verdict["saved_at"] = spark_web_api.cookies_saved_at()
    return verdict
