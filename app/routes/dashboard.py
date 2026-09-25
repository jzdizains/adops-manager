"""Home / overview: KPI cards (Spend / Revenue / ROAS / Profit — revenue is
REAL from ConversionSample), metric-comparison chart, Top Ad Accounts, Active
Campaigns with 'synced X ago', System Status probing real cookie health.
Also /accounts (the synced list) and /admin/cookie-check."""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import case, func

from .. import models, pnl_data, queries, spark_web_api, timeutil
from ..database import get_db
from ..templating import render

from . import guard
from .. import scope as scope_mod

router = APIRouter()


def _count_launched(db: Session, since_utc, ids) -> int:
    q = db.query(models.LaunchLog).filter(models.LaunchLog.ok == True, models.LaunchLog.created_at >= since_utc.replace(tzinfo=None))  # noqa: E712
    if ids is not None:
        q = q.filter(models.LaunchLog.advertiser_id.in_(list(ids or [""])))
    return q.count()


@router.get("/")
def overview(request: Request, db: Session = Depends(get_db)):
    """Home = the live command center: profit first, today by hour vs
    yesterday, which creatives won / lost today, what needs attention, and
    the Business Centers at a glance. Live tiles poll /performance/data."""
    from datetime import timedelta
    from .. import creative_perf, hourly
    from .. import inbox as inbox_mod
    from . import super_launcher as sl

    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)                 # whose dashboard this is
    ids = sc.ids                                            # None = everyone
    start_utc, end_utc = timeutil.range_bounds("today")
    y_start, y_end = timeutil.range_bounds("yesterday")
    kpis = pnl_data.overall_totals(db, start_utc, end_utc, ids)
    ykpis = pnl_data.overall_totals(db, y_start, y_end, ids)
    cq = db.query(func.coalesce(func.sum(models.CampaignRecord.clicks), 0))
    if ids is not None:
        cq = cq.filter(models.CampaignRecord.advertiser_id.in_(list(ids or [""])))
    tt_clicks = int(cq.scalar() or 0)
    kpis["tt_clicks"] = tt_clicks
    kpis["epc"] = (kpis["revenue"] / tt_clicks) if tt_clicks else 0.0
    y_tt_clicks = 0     # TikTok clicks aren't snapshotted per day; EPC delta uses postback clicks
    kpis["epc_pb"] = (kpis["revenue"] / kpis["clicks"]) if kpis["clicks"] else 0.0
    ykpis["epc_pb"] = (ykpis["revenue"] / ykpis["clicks"]) if ykpis["clicks"] else 0.0
    # pace of the day: profit at this hour yesterday (so +38% means vs the same time)
    today_day, y_day = timeutil.local_date_str(start_utc), timeutil.local_date_str(y_start)
    h_now = timeutil.now_local().hour
    recs_q = db.query(models.CampaignRecord.campaign_id, models.CampaignRecord.advertiser_id)
    if ids is not None:
        recs_q = recs_q.filter(models.CampaignRecord.advertiser_id.in_(list(ids or [""])))
    all_ids = [r[0] for r in recs_q]
    ht, hy = hourly.series(db, all_ids, today_day), hourly.series(db, all_ids, y_day)
    if ids is None:
        rt, ry = hourly.revenue_series(db, None, today_day), hourly.revenue_series(db, None, y_day)
    else:
        wt = pnl_data.source_weights(db, start_utc, end_utc, ids)
        wy = pnl_data.source_weights(db, y_start, y_end, ids)
        rt, ry = hourly.revenue_series(db, None, today_day, wt), hourly.revenue_series(db, None, y_day, wy)
    y_same_hour = {"spend": sum(hy["spend"][:h_now + 1]), "revenue": sum(ry[:h_now + 1])}
    y_same_hour["profit"] = y_same_hour["revenue"] - y_same_hour["spend"]
    hourly_json = json.dumps({"today": {"spend": ht["spend"], "revenue": rt, "conversions": ht["conversions"], "clicks": ht["clicks"]},
                              "yesterday": {"spend": hy["spend"], "revenue": ry, "conversions": hy["conversions"], "clicks": hy["clicks"]},
                              "hour_now": h_now, "has_hourly": hourly.days_available(db) > 0})

    # ---- today's creative tests: families rolled up, winners / learning / losing
    perf = creative_perf.rows(db, start_utc, end_utc, today=True, owner_user_id=sc.user_id)
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
             "launched_today": _count_launched(db, start_utc, ids)}
    winners = [f for f in fams if f["cls"] == "winner"][:5]
    losers = sorted([f for f in fams if f["cls"] == "losing"], key=lambda f: f["profit"])[:5]

    # ---- attention (unified inbox) + Business Centers -------------------------
    inbox_items = inbox_mod.build(db, sc)
    inbox_counts = inbox_mod.counts(inbox_items)
    all_accounts = db.query(models.AdAccount).all()
    accounts = [a for a in all_accounts if sc.allows(a.advertiser_id)]
    ctx = sl.account_picker_context(db, accounts)
    bcs = [b for b in db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all() if not b.retired]
    spend_by_aid = {r[0]: float(r[1] or 0) for r in db.query(models.CampaignRecord.advertiser_id, func.sum(models.CampaignRecord.spend_today)).group_by(models.CampaignRecord.advertiser_id)}
    src_map = pnl_data.campaign_source_map(db, ids)
    pb = pnl_data.revenue_by_source(db, start_utc, end_utc, ids)
    rev_by_aid: dict[str, float] = {}
    for r in db.query(models.CampaignRecord).all():
        src = src_map.get(r.campaign_id, "")
        if src and src in pb:
            rev_by_aid[r.advertiser_id] = rev_by_aid.get(r.advertiser_id, 0.0) + float(pb[src].get("revenue", 0.0))
    bc_rows = []
    for b in bcs:
        # a user's Home lists the Business Centers their accounts sit in (wallets are shared)
        aids = [a.advertiser_id for a in accounts if a.owner_bc_id == b.bc_id]
        if ids is not None and not aids:
            continue
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
        "view": sc,
    })


@router.get("/accounts")
def accounts_page(request: Request, db: Session = Depends(get_db)):
    """Every ad account grouped by Business Center: state (fresh/live/used/
    cooling/blocked), balance, spend today, revenue → profit + ROAS, campaigns,
    last launch. Filtering happens in the browser (accounts.js)."""
    from . import super_launcher as sl
    from .. import activity as activity_mod, balances as bal_mod
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    show_lost = request.query_params.get("show_lost") == "1"
    all_bcs = db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()
    retired_ids = {b.bc_id for b in all_bcs if b.retired}          # v155.27: removed Business Centers stay out of sight
    all_accounts = [a for a in db.query(models.AdAccount).order_by(models.AdAccount.advertiser_name).all() if sc.allows(a.advertiser_id)]
    lost = [a for a in all_accounts if a.status == "ACCESS_LOST" and (a.owner_bc_id or "") not in retired_ids]
    retired_accts = [a for a in all_accounts if (a.owner_bc_id or "") in retired_ids]
    accounts = all_accounts if show_lost else [a for a in all_accounts if a.status != "ACCESS_LOST" and (a.owner_bc_id or "") not in retired_ids]
    ctx = sl.account_picker_context(db, accounts)
    facts = _account_facts(db, accounts, ctx)
    bcs = all_bcs if show_lost else [b for b in all_bcs if not b.retired]
    by_bc: dict[str, list] = {}
    for a in accounts:
        by_bc.setdefault(a.owner_bc_id or "", []).append(a)
    groups = []
    for b in bcs + [None]:
        key = b.bc_id if b else ""
        members = by_bc.get(key, [])
        if not members and (b is None or not sc.everything):
            continue                      # a user's page lists only the Business Centers their accounts sit in
        # profit first inside a BC, then fresh accounts, then the rest by name
        members.sort(key=lambda a: (-facts[a.advertiser_id]["profit"], 0 if facts[a.advertiser_id]["state"] == "fresh" else 1, (a.advertiser_name or "").lower()))
        st = [facts[a.advertiser_id]["state"] for a in members]
        sp = sum(facts[a.advertiser_id]["spend"] for a in members); rv = sum(facts[a.advertiser_id]["revenue"] for a in members)
        thr = bal_mod.bc_threshold(b) if b else 0.0
        groups.append({"bc": b, "key": key, "name": b.name if b else "No Business Center", "members": members, "retired": bool(b and b.retired),
                       "can_remove": bool(b) and (sc.everything or all(sc.allows(a.advertiser_id) for a in members)),
                       "n": len(members), "fresh": st.count("fresh"), "live": st.count("active"), "blocked": st.count("blocked") + st.count("cooldown"),
                       "spend": sp, "revenue": rv, "profit": rv - sp, "low": bool(b) and (b.balance or 0) < thr, "threshold": thr,
                       "block": sl.bc_block(b) if b else "", "portal": bal_mod.bc_portal_url(b.bc_id) if b else ""})
    counts = dict(ctx["counts"]); counts["off"] = sum(1 for a in accounts if not a.enabled)
    counts["low"] = sum(1 for a in accounts if a.balance is not None and a.enabled and (a.balance or 0) < 20)
    notes = activity_mod.notes_for(db, "account", [a.advertiser_id for a in accounts])
    people = scope_mod.users_index(db) if sc.can_switch else {}
    return render(request, "accounts.html", {
        "accounts": accounts, "title": "Ad accounts", "facts": facts, "counts": counts, "groups": groups, "notes": notes,
        "view": sc, "people": people, "people_sorted": sorted(people.values(), key=lambda u: u.email),
        "lost_count": len(lost), "show_lost": show_lost, "n_bc": len(bcs), "retired_count": len(retired_ids), "retired_accts": len(retired_accts),
        "logins": _logins_for_page(db, sc, people),
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


@router.post("/accounts/owner")
async def accounts_owner(request: Request, db: Session = Depends(get_db)):
    """Super admin: move ad accounts to a user's workspace. Form: advertiser_ids
    (repeated or comma-joined), user_id. The campaigns, spend, postbacks and audience
    data of those accounts move with them — it's a filter, nothing is copied."""
    from fastapi.responses import JSONResponse
    from .. import scope as scope_mod, users as users_mod
    me = getattr(request.state, "user", None)
    if not users_mod.is_owner(me):
        return JSONResponse({"ok": False, "error": "only the super admin can move accounts between users"}, status_code=403)
    form = await request.form()
    ids: list[str] = []
    for v in form.getlist("advertiser_ids"):
        ids.extend(x.strip() for x in str(v).split(",") if x.strip())
    ids = list(dict.fromkeys(ids))[:2000]
    try:
        uid = int(form.get("user_id") or 0)
    except (TypeError, ValueError):
        uid = 0
    target = db.get(models.User, uid) if uid else None
    if target is None or not ids:
        return JSONResponse({"ok": False, "error": "pick a user and at least one account"}, status_code=400)
    n = (db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(ids))
         .update({models.AdAccount.owner_user_id: target.id}, synchronize_session=False))
    db.commit()
    from .. import activity as _activity
    _activity.record(db, "account", ids[0] if len(ids) == 1 else "bulk", "owner",
                     f"{n} account(s) → {target.email}", request=request)
    if request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "moved": int(n or 0), "owner": {"id": target.id, "email": target.email}})
    return RedirectResponse(f"/accounts?ok={n}+account(s)+moved+to+{target.email}", status_code=303)


@router.post("/accounts/geo-policy")
async def accounts_geo_policy(request: Request, db: Session = Depends(get_db)):
    """Set what geos accounts are FOR (v151): any | auto | us_only | non_us. Form: advertiser_ids
    (comma-joined), policy. Only accounts in the caller's view are changed."""
    from fastapi.responses import JSONResponse
    from .. import geo_fit
    form = await request.form()
    policy = str(form.get("policy") or "")
    if policy not in geo_fit.POLICY_KEYS:
        return JSONResponse({"ok": False, "error": "unknown geo policy"}, status_code=400)
    sc = scope_mod.for_request(request, db)
    ids = [x for x in dict.fromkeys(x.strip() for x in str(form.get("advertiser_ids") or "").split(",")) if x and sc.allows(x)][:2000]
    if not ids:
        return JSONResponse({"ok": False, "error": "pick at least one account"}, status_code=400)
    n = (db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(ids))
         .update({models.AdAccount.geo_policy: policy}, synchronize_session=False))
    db.commit()
    from .. import activity as _activity
    _activity.record(db, "account", ids[0] if len(ids) == 1 else "bulk", "geo_policy",
                     f"{n} account(s) → {policy}", request=request)
    return JSONResponse({"ok": True, "changed": int(n or 0), "policy": policy,
                         "label": dict((k, l) for k, l, _ in geo_fit.POLICIES)[policy]})


def _logins_for_page(db: Session, sc, people: dict) -> list[dict]:
    """The connected TikTok logins of this view (v155.39) — name, TikTok email, whose workspace,
    what the last sync found — so "my BCs don't show up" is answerable from the page itself."""
    try:
        rows = queries.logins(db, None if sc.everything else sc.user_id)
    except Exception:  # noqa: BLE001
        db.rollback()
        return []
    out = []
    for lg in rows:
        u = people.get(lg.user_id) if people else None
        out.append({"name": lg.display_name or "(no name yet)", "email": lg.email or "", "user": (u.email if u else ""),
                    "result": lg.last_result or "", "at": lg.last_synced_at})
    return out


def _retire_bc(db: Session, sc, bc_id: str, retire: bool):
    """v155.27 — Remove / restore a Business Center the operator no longer uses. Removing hides
    it and its accounts from every page and picker, switches those accounts off (no launcher
    touches them) and stops the balance / alert sweeps for it; nothing is deleted — history,
    P&L and launches stay, and Restore brings it all back. Returns (bc, accounts touched, error)."""
    b = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
    if b is None:
        return None, 0, "No such Business Center."
    members = list(db.query(models.AdAccount).filter(models.AdAccount.owner_bc_id == bc_id))
    if not (sc.everything or (members and all(sc.allows(a.advertiser_id) for a in members))):
        return None, 0, "That Business Center isn't in your view."
    n = 0
    for a in members:
        if retire and a.enabled:
            a.enabled = False
            n += 1
        elif not retire and not a.enabled and a.status != "ACCESS_LOST":
            a.enabled = True
            n += 1
    b.retired = bool(retire)
    db.commit()
    return b, n, ""


@router.post("/accounts/bc/{bc_id}/remove")
def bc_remove(request: Request, bc_id: str, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    b, n, err = _retire_bc(db, sc, bc_id, True)
    if err:
        return RedirectResponse("/accounts?err=" + quote(err), status_code=303)
    from .. import audit
    audit.from_request(db, models, request, "bc.removed", target=b.name or bc_id, detail=f"{n} account(s) switched off")
    return RedirectResponse("/accounts?ok=" + quote(f"Removed {b.name or bc_id} — {n} account(s) switched off and hidden. Undo under “hidden”."), status_code=303)


@router.post("/accounts/bc/{bc_id}/restore")
def bc_restore(request: Request, bc_id: str, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    b, n, err = _retire_bc(db, sc, bc_id, False)
    if err:
        return RedirectResponse("/accounts?err=" + quote(err), status_code=303)
    from .. import audit
    audit.from_request(db, models, request, "bc.restored", target=b.name or bc_id, detail=f"{n} account(s) switched on")
    return RedirectResponse("/accounts?ok=" + quote(f"Restored {b.name or bc_id} — {n} account(s) switched back on."), status_code=303)


@router.get("/accounts/bc/{bc_id}/detail")
def bc_detail(request: Request, bc_id: str, db: Session = Depends(get_db)):
    """One Business Center for the Home drawer: its accounts with state, spend, profit — no page change.
    Only the accounts in the caller's view (v151 audit)."""
    from . import super_launcher as sl
    from .. import balances as bal_mod
    sc = scope_mod.for_request(request, db)
    b = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
    accounts = [a for a in db.query(models.AdAccount).filter(models.AdAccount.owner_bc_id == bc_id).order_by(models.AdAccount.advertiser_name)
                if a.status != "ACCESS_LOST" and sc.allows(a.advertiser_id)]
    if not accounts:
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
def account_detail(advertiser_id: str, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """JSON for the account drawer: facts, today's campaigns, BC, note, recent launches."""
    from fastapi.responses import JSONResponse
    from . import super_launcher as sl
    from .. import activity as activity_mod, geo_fit as _geo
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
        "note": (lambda n: n.text if n else "")(activity_mod.get_note(db, "account", a.advertiser_id)),
        "geo_policy": a.geo_policy or "any", "geo_badge": _geo.badge(_geo.cached_targetable(db, a.advertiser_id)),
        "geo_policies": [{"value": k, "label": l, "hint": h} for k, l, h in _geo.POLICIES],
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
def toggle_account(request: Request, advertiser_id: str, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    from fastapi.responses import JSONResponse
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct:
        acct.enabled = not acct.enabled
        db.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": bool(acct), "enabled": bool(acct and acct.enabled)})
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{advertiser_id}/transfer")
async def account_transfer(request: Request, advertiser_id: str, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
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
        from starlette.concurrency import run_in_threadpool
        await run_in_threadpool(tiktok_api.bc_transfer, acct.access_token, bc.bc_id, acct.advertiser_id, amount, "RECHARGE")
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
