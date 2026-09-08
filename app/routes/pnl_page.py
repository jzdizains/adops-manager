"""P&L — profit first. One page: KPI strip (profit vs the prior period),
profit per day (or per hour for a single day), then ONE breakdown card whose
tabs slice the same numbers by source / Business Center / account / creative /
spark, plus the raw postback feed. CSV export of any slice.

Numbers: spend = SpendSnapshot (every campaign, sourced or not); revenue =
PostbackEvent. Revenue reaches an account / BC / creative through the
campaign→source map; a source shared by several campaigns is split by spend
share (the same rule the Campaigns page uses)."""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import creative_perf, hourly, models, pnl_data, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()

RANGE_LABELS = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days", "30d": "Last 30 days", "mtd": "This month", "custom": "Custom range"}


def _slices(db: Session, start_utc, end_utc) -> dict:
    """Everything the page and the CSV export need for one range."""
    from .postback import UNATTRIBUTED, _goals_by_source
    start_day, end_day = timeutil.local_date_str(start_utc), timeutil.local_date_str(end_utc - timeutil.timedelta(seconds=1))
    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)
    # spend per campaign in range
    spend_by_cid: dict[str, float] = {}
    for cid, sp in (db.query(models.SpendSnapshot.campaign_id, func.sum(models.SpendSnapshot.spend))
                    .filter(models.SpendSnapshot.day >= start_day, models.SpendSnapshot.day <= end_day)
                    .group_by(models.SpendSnapshot.campaign_id)):
        spend_by_cid[cid] = float(sp or 0)
    # revenue per source in range
    rev_by_src: dict[str, dict] = {}
    for src, rv, cv, ck in (db.query(models.PostbackEvent.source, func.sum(models.PostbackEvent.revenue), func.sum(models.PostbackEvent.conversions), func.sum(models.PostbackEvent.clicks))
                            .filter(models.PostbackEvent.created_at >= s_naive, models.PostbackEvent.created_at < e_naive)
                            .group_by(models.PostbackEvent.source)):
        rev_by_src[src] = {"revenue": float(rv or 0), "conversions": int(cv or 0), "clicks": int(ck or 0)}
    src_map = pnl_data.campaign_source_map(db)            # campaign → source
    camps = {c.campaign_id: c for c in db.query(models.CampaignRecord).all()}
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    cids_by_src: dict[str, list[str]] = {}
    for cid, src in src_map.items():
        cids_by_src.setdefault(src, []).append(cid)

    # ---- by source ----------------------------------------------------------------------------
    sparks = {sp.source: sp for sp in db.query(models.SparkCode).filter(models.SparkCode.source != "") if sp.source}
    src_rows = []
    for src in set(rev_by_src) | set(cids_by_src):
        if src == UNATTRIBUTED:
            continue
        cids = cids_by_src.get(src, [])
        sp = sum(spend_by_cid.get(c, 0.0) for c in cids)
        r = rev_by_src.get(src, {"revenue": 0.0, "conversions": 0, "clicks": 0})
        if not sp and not r["revenue"]:
            continue
        accs = {camps[c].advertiser_id for c in cids if c in camps}
        src_rows.append({"key": src, "name": src, "sub": (sparks[src].name if src in sparks and sparks[src].name else "") or (f"{len(cids)} campaign{'s' if len(cids) != 1 else ''}" if cids else "no campaign matches"),
                         "spend": sp, "revenue": r["revenue"], "profit": r["revenue"] - sp, "conversions": r["conversions"], "clicks": r["clicks"],
                         "n": len(cids), "accounts": len(accs), "spark": sparks[src].name if src in sparks else "", "href": f"/status?source={src}&origin=all"})
    unattributed = rev_by_src.get(UNATTRIBUTED, {"revenue": 0.0, "conversions": 0, "clicks": 0})

    # ---- revenue allocated to campaigns (spend share) → account / BC ----------------------------
    rev_by_cid: dict[str, float] = {}
    conv_by_cid: dict[str, float] = {}
    for src, cids in cids_by_src.items():
        r = rev_by_src.get(src)
        if not r or not cids:
            continue
        tot = sum(spend_by_cid.get(c, 0.0) for c in cids)
        for c in cids:
            share = (spend_by_cid.get(c, 0.0) / tot) if tot else 1.0 / len(cids)
            rev_by_cid[c] = rev_by_cid.get(c, 0.0) + r["revenue"] * share
            conv_by_cid[c] = conv_by_cid.get(c, 0.0) + r["conversions"] * share
    acc_rows_d: dict[str, dict] = {}
    for cid in set(spend_by_cid) | set(rev_by_cid):
        c = camps.get(cid)
        aid = c.advertiser_id if c else ""
        a = accounts.get(aid)
        row = acc_rows_d.setdefault(aid, {"key": aid, "name": (a.advertiser_name if a else "") or aid or "unknown account", "sub": "", "spend": 0.0, "revenue": 0.0, "conversions": 0.0, "clicks": 0, "n": 0, "bc": (a.owner_bc_id if a else "") or "", "href": f"/status?account={aid}&origin=all"})
        row["spend"] += spend_by_cid.get(cid, 0.0); row["revenue"] += rev_by_cid.get(cid, 0.0); row["conversions"] += conv_by_cid.get(cid, 0.0); row["n"] += 1
    acc_rows = []
    for row in acc_rows_d.values():
        b = bcs.get(row["bc"])
        row["sub"] = (b.name if b else "no Business Center") + f" · {row['n']} campaign{'s' if row['n'] != 1 else ''}"
        row["profit"] = row["revenue"] - row["spend"]; row["conversions"] = int(round(row["conversions"]))
        acc_rows.append(row)
    bc_rows_d: dict[str, dict] = {}
    for row in acc_rows:
        b = bcs.get(row["bc"])
        g = bc_rows_d.setdefault(row["bc"], {"key": row["bc"], "name": (b.name if b else "") or "No Business Center", "sub": "", "spend": 0.0, "revenue": 0.0, "conversions": 0, "clicks": 0, "n": 0, "accounts": 0, "href": "/accounts"})
        g["spend"] += row["spend"]; g["revenue"] += row["revenue"]; g["conversions"] += row["conversions"]; g["n"] += row["n"]; g["accounts"] += 1
    bc_rows = []
    for g in bc_rows_d.values():
        g["sub"] = f"{g['accounts']} account{'s' if g['accounts'] != 1 else ''} · {g['n']} campaign{'s' if g['n'] != 1 else ''}"; g["profit"] = g["revenue"] - g["spend"]
        bc_rows.append(g)

    # ---- by creative (families) ---------------------------------------------------------------
    cr_rows = []
    for f in creative_perf.families(creative_perf.rows(db, start_utc, end_utc)):
        if not f["spend"] and not f["revenue"]:
            continue
        best = f["best"]["c"] if f["best"] else None
        cr_rows.append({"key": f["key"], "name": f["name"], "sub": f"{f['n']} variant{'s' if f['n'] != 1 else ''}" + (f" · {best.kind}" if best else ""), "spend": f["spend"], "revenue": f["revenue"], "profit": f["profit"],
                        "conversions": sum(int(r.get("conversions", 0) or 0) for r in f["rows"]), "clicks": 0, "n": f["n"], "href": f"/creatives?view=results&sort=profit", "cid": best.id if best else 0})
    # ---- by spark -----------------------------------------------------------------------------
    sp_rows_d: dict[str, dict] = {}
    for r in src_rows:
        if not r["spark"]:
            continue
        g = sp_rows_d.setdefault(r["spark"], {"key": r["spark"], "name": r["spark"], "sub": "", "spend": 0.0, "revenue": 0.0, "conversions": 0, "clicks": 0, "n": 0, "href": "/spark-codes"})
        g["spend"] += r["spend"]; g["revenue"] += r["revenue"]; g["conversions"] += r["conversions"]; g["clicks"] += r["clicks"]; g["n"] += 1
    sp_rows = []
    for g in sp_rows_d.values():
        g["sub"] = f"{g['n']} source{'s' if g['n'] != 1 else ''}"; g["profit"] = g["revenue"] - g["spend"]; sp_rows.append(g)

    for rows in (src_rows, acc_rows, bc_rows, cr_rows, sp_rows):
        for r in rows:
            r["roas"] = (r["revenue"] / r["spend"]) if r["spend"] else 0.0
        rows.sort(key=lambda r: r["profit"], reverse=True)
    goals = _goals_by_source(db, [r["key"] for r in src_rows])
    for r in src_rows:
        r["goal"] = goals.get(r["key"], "")
    return {"source": src_rows, "bc": bc_rows, "account": acc_rows, "creative": cr_rows, "spark": sp_rows,
            "unattributed": unattributed, "unsourced_spend": sum(sp for cid, sp in spend_by_cid.items() if cid not in src_map)}


@router.get("/pnl")
def pnl(request: Request, db: Session = Depends(get_db)):
    range_key = request.query_params.get("range", "today")
    start, end = request.query_params.get("start"), request.query_params.get("end")
    start_utc, end_utc = timeutil.range_bounds(range_key, start, end)
    length = end_utc - start_utc
    totals = pnl_data.overall_totals(db, start_utc, end_utc)
    prior = pnl_data.overall_totals(db, start_utc - length, start_utc)
    slices = _slices(db, start_utc, end_utc)
    n_days = max(1, int(round(length.total_seconds() / 86400)))
    # chart: per day for multi-day ranges, per hour for a single day
    if n_days > 1:
        days, sp, rv = pnl_data.daily_series(db, start_utc, end_utc)
        chart = {"mode": "day", "labels": days, "spend": sp, "revenue": rv}
    else:
        day = timeutil.local_date_str(start_utc)
        cids = [r[0] for r in db.query(models.CampaignRecord.campaign_id).all()]
        hs = hourly.series(db, cids, day)
        chart = {"mode": "hour", "labels": [f"{h}h" for h in range(24)], "spend": hs["spend"], "revenue": hourly.revenue_series(db, None, day),
                 "hour_now": timeutil.now_local().hour if range_key == "today" else 24}
    profits = [r - s for s, r in zip(chart["spend"], chart["revenue"])]
    best_i = max(range(len(profits)), key=lambda i: profits[i]) if profits and any(profits) else -1
    best = None
    if best_i >= 0:
        lab = chart["labels"][best_i]
        if chart["mode"] == "day":
            from datetime import datetime as _dt
            lab = _dt.strptime(lab, "%Y-%m-%d").strftime("%a %b %d")
        best = {"label": lab, "profit": profits[best_i]}
    def delta(cur, prev):
        return ((cur - prev) / abs(prev) * 100) if prev else None
    active_accounts = db.query(func.count(func.distinct(models.SpendSnapshot.advertiser_id))).filter(
        models.SpendSnapshot.day >= timeutil.local_date_str(start_utc), models.SpendSnapshot.day <= timeutil.local_date_str(end_utc - timeutil.timedelta(seconds=1)), models.SpendSnapshot.spend > 0).scalar() or 0
    recent = db.query(models.PostbackEvent).order_by(models.PostbackEvent.created_at.desc()).limit(40).all()
    from .postback import UNATTRIBUTED
    import difflib
    known: dict[str, models.LaunchLog] = {}
    for lg in (db.query(models.LaunchLog).filter(models.LaunchLog.ok == True, models.LaunchLog.source != "").order_by(models.LaunchLog.id.desc())):  # noqa: E712
        known.setdefault(lg.source, lg)
    names = {r.campaign_id: r.campaign_name for r in db.query(models.CampaignRecord).all()}
    match: dict[int, dict] = {}
    for e in recent:
        if e.source == UNATTRIBUTED:
            match[e.id] = {"state": "unattributed"}
        elif e.source in known:
            lg = known[e.source]
            match[e.id] = {"state": "ok", "advertiser_id": lg.advertiser_id, "campaign_id": lg.campaign_id, "name": names.get(lg.campaign_id) or lg.source}
        else:
            close = difflib.get_close_matches(e.source, list(known), n=1, cutoff=0.6)
            match[e.id] = {"state": "nomatch", "closest": close[0] if close else ""}
    tab = request.query_params.get("by", "source")
    if tab not in ("source", "bc", "account", "creative", "spark", "postbacks"):
        tab = "source"
    return render(request, "pnl.html", {
        "title": "P&L", "range_key": range_key, "range_label": RANGE_LABELS.get(range_key, "Custom range"), "start": start or "", "end": end or "",
        "totals": totals, "prior": prior, "d_profit": delta(totals["profit"], prior["profit"]), "d_rev": delta(totals["revenue"], prior["revenue"]), "d_spend": delta(totals["spend"], prior["spend"]),
        "epc": (totals["revenue"] / totals["clicks"]) if totals["clicks"] else 0.0, "active_accounts": active_accounts, "best": best, "n_days": n_days,
        "chart_json": json.dumps(chart), "slices": slices, "tab": tab, "recent": recent, "match": match,
        "winners": sum(1 for r in slices["source"] if r["profit"] > 0), "n_sources": len(slices["source"]),
        "qs": f"range={range_key}" + (f"&start={start}&end={end}" if start and end else ""),
    })


@router.get("/pnl/export.csv")
def pnl_export(request: Request, db: Session = Depends(get_db)):
    range_key = request.query_params.get("range", "today")
    start_utc, end_utc = timeutil.range_bounds(range_key, request.query_params.get("start"), request.query_params.get("end"))
    by = request.query_params.get("by", "source")
    slices = _slices(db, start_utc, end_utc)
    rows = slices.get(by if by in ("source", "bc", "account", "creative", "spark") else "source", [])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([by, "detail", "spend", "revenue", "profit", "roas", "conversions", "clicks"])
    for r in rows:
        w.writerow([r["name"], r["sub"], f"{r['spend']:.2f}", f"{r['revenue']:.2f}", f"{r['profit']:.2f}", f"{r['roas']:.2f}", r["conversions"], r["clicks"]])
    fname = f"pnl_{by}_{timeutil.local_date_str(start_utc)}_{timeutil.local_date_str(end_utc - timeutil.timedelta(seconds=1))}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{fname}"'})
