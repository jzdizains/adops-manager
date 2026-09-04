"""Campaigns manager — every campaign across every account with on/off toggle,
full TikTok metrics (spend, impressions, clicks, CTR, CPC, CPM, conversions,
CPA), Glitchy postback performance (revenue, clicks, CVR, profit) joined per
campaign through its source, filters, sortable columns, and a manual sync.

Postback attribution note: Glitchy reports per SOURCE (one source = one spark
code). When the same spark was launched to several accounts, all those
campaigns share one source — revenue/clicks are apportioned by each campaign's
share of the source's spend (even split when nothing has spent yet), and the
row is marked 'split ÷N'. CVR is shown at source level (a ratio survives the
split unchanged)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import live_spend, models, pnl_data, queries, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()

# sort key -> how to read the value from a built row dict
SORT_KEYS = {
    "spend": lambda row: row["m"]["spend"],
    "impr": lambda row: row["m"]["impressions"],
    "clicks": lambda row: row["m"]["clicks"],
    "ctr": lambda row: row["m"]["ctr"],
    "cpc": lambda row: row["m"]["cpc"],
    "cpm": lambda row: row["m"]["cpm"],
    "conv": lambda row: row["m"]["conversions"],
    "cpa": lambda row: row["m"]["cpa"],
    "budget": lambda row: row["r"].budget,
    "name": lambda row: (row["r"].campaign_name or "").lower(),
    "source": lambda row: row["source"],
    "revenue": lambda row: row["revenue"],
    "pb_clicks": lambda row: row["pb_clicks"],
    "cvr": lambda row: row["cvr"],
    "profit": lambda row: row["profit"],
}


@router.get("/status")
def status_page(request: Request, db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip().lower()
    state = request.query_params.get("state", "all")          # all | active | paused
    account = request.query_params.get("account", "")          # advertiser_id
    source_f = request.query_params.get("source", "").strip()  # P&L source filter
    origin = request.query_params.get("origin", "tool")        # tool | all
    range_key = request.query_params.get("range", "today")
    start = request.query_params.get("start") or None
    end = request.query_params.get("end") or None
    if range_key not in ("today", "yesterday", "7d", "30d", "mtd", "custom"):
        range_key = "today"
    sort = request.query_params.get("sort", "spend")
    if sort not in SORT_KEYS:
        sort = "spend"

    records = db.query(models.CampaignRecord).all()
    # campaigns this tool launched (successful launches carry the campaign id)
    tool_campaign_ids = {log.campaign_id for log in
                         db.query(models.LaunchLog.campaign_id)
                         .filter(models.LaunchLog.ok == True,          # noqa: E712
                                 models.LaunchLog.campaign_id != "",
                                 ~models.LaunchLog.campaign_id.startswith("deleted:"))}
    cached_ids = {r.campaign_id for r in records}
    pending_ids = tool_campaign_ids - cached_ids         # launched, not synced yet
    pending_tool = len(pending_ids)
    pending_details: list[dict] = []
    if pending_ids:
        import json as _json
        try:
            report = _json.loads(queries.get_setting(db, "campaign_sync_report") or "{}")
        except (ValueError, TypeError):
            report = {}
        err_by_acct = {e.get("advertiser_id"): e for e in report.get("errors", [])}
        acct_names = {a.advertiser_id: (a.advertiser_name or a.advertiser_id)
                      for a in db.query(models.AdAccount).all()}
        for log_row in (db.query(models.LaunchLog)
                        .filter(models.LaunchLog.campaign_id.in_(list(pending_ids)))):
            e = err_by_acct.get(log_row.advertiser_id)
            pending_details.append({
                "campaign_id": log_row.campaign_id,
                "account": acct_names.get(log_row.advertiser_id, log_row.advertiser_id),
                "sync_error": (f"code {e['code']}: {e['message']}" if e else ""),
            })
    if origin == "tool":
        records = [r for r in records if r.campaign_id in tool_campaign_ids]
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    sources = pnl_data.campaign_source_map(db)

    # --- Glitchy postback truth for the selected range, per source -------------
    start_utc, end_utc = timeutil.range_bounds(range_key, start, end)
    pb = pnl_data.revenue_by_source(db, start_utc, end_utc)

    # --- TikTok metrics for the range: today = the synced cache (fast, free);
    #     any other range = one live report call per account in view ----------
    from datetime import timedelta as _td

    from .. import tiktok_api
    range_errors = 0
    metrics_by_cid: dict | None = None
    if range_key != "today":
        metrics_by_cid = {}
        s_day = timeutil.local_date_str(start_utc)
        e_day = timeutil.local_date_str(end_utc - _td(seconds=1))
        for aid in {r.advertiser_id for r in records}:
            a = accounts.get(aid)
            if not a or not a.access_token:
                continue
            try:
                for rr in tiktok_api.get_report(
                        a.access_token, aid, dimensions=["campaign_id"],
                        metrics=live_spend.REPORT_METRICS,
                        start_date=s_day, end_date=e_day):
                    cid = str(rr.get("dimensions", {}).get("campaign_id", ""))
                    metrics_by_cid[cid] = rr.get("metrics", {}) or {}
            except tiktok_api.TikTokError:
                range_errors += 1

    def _metrics(rec) -> dict:
        if metrics_by_cid is None:      # today → cached values
            return {"spend": float(rec.spend_today or 0), "impressions": rec.impressions or 0,
                    "clicks": rec.clicks or 0, "conversions": rec.conversions or 0,
                    "ctr": rec.ctr or 0.0, "cpc": rec.cpc or 0.0,
                    "cpm": rec.cpm or 0.0, "cpa": rec.cpa or 0.0}
        mm = metrics_by_cid.get(rec.campaign_id, {})
        f = live_spend._f
        return {"spend": f(mm, "spend"), "impressions": int(f(mm, "impressions")),
                "clicks": int(f(mm, "clicks")), "conversions": int(f(mm, "conversion")),
                "ctr": f(mm, "ctr"), "cpc": f(mm, "cpc"),
                "cpm": f(mm, "cpm"), "cpa": f(mm, "cost_per_conversion")}

    # how many campaigns share each source + that source's total spend in range
    # (computed over ALL records so filters never change the apportioning)
    src_count: dict[str, int] = {}
    src_spend: dict[str, float] = {}
    metrics_cache: dict[str, dict] = {}
    for r in records:
        metrics_cache[r.campaign_id] = _metrics(r)
        src = sources.get(r.campaign_id, "")
        if src:
            src_count[src] = src_count.get(src, 0) + 1
            src_spend[src] = src_spend.get(src, 0.0) + metrics_cache[r.campaign_id]["spend"]

    rows = []
    for r in records:
        acct = accounts.get(r.advertiser_id)
        name = (acct.advertiser_name if acct else r.advertiser_id) or r.advertiser_id
        if q and q not in r.campaign_name.lower() and q not in name.lower():
            continue
        if state == "active" and r.operation_status != "ENABLE":
            continue
        if state == "paused" and r.operation_status != "DISABLE":
            continue
        if account and r.advertiser_id != account:
            continue
        if source_f and sources.get(r.campaign_id, "") != source_f:
            continue

        m = metrics_cache[r.campaign_id]
        src = sources.get(r.campaign_id, "")
        src_pb = pb.get(src, {}) if src else {}
        n = src_count.get(src, 1)
        if src and n > 1:
            total = src_spend.get(src, 0.0)
            share = (m["spend"] / total) if total > 0 else (1.0 / n)
        else:
            share = 1.0
        revenue = float(src_pb.get("revenue", 0.0)) * share
        pb_clicks = float(src_pb.get("clicks", 0)) * share
        pb_conv = float(src_pb.get("conversions", 0)) * share
        src_clicks = int(src_pb.get("clicks", 0))
        src_conv = int(src_pb.get("conversions", 0))
        rows.append({
            "r": r, "m": m, "account_name": name, "source": src,
            "shared_n": n if (src and n > 1) else 0,
            "revenue": revenue,
            "pb_clicks": pb_clicks,
            "pb_conversions": pb_conv,
            # CVR is a ratio → identical for every campaign on the source
            "cvr": (src_conv / src_clicks * 100) if src_clicks else 0.0,
            "profit": revenue - m["spend"],
        })

    reverse = sort not in ("name", "source")
    keyfn = SORT_KEYS[sort]
    rows.sort(key=lambda row: keyfn(row) or (0 if reverse else ""), reverse=reverse)

    # totals across the FILTERED rows; rate metrics recomputed from the sums so
    # they're properly weighted (never an average of averages)
    spend = sum(row["m"]["spend"] for row in rows)
    impressions = sum(row["m"]["impressions"] for row in rows)
    clicks = sum(row["m"]["clicks"] for row in rows)
    conversions = sum(row["m"]["conversions"] for row in rows)
    revenue = sum(row["revenue"] for row in rows)
    pb_clicks = sum(row["pb_clicks"] for row in rows)
    pb_conversions = sum(row["pb_conversions"] for row in rows)
    totals = {
        "spend": spend, "impressions": impressions, "clicks": clicks,
        "conversions": conversions,
        "ctr": (clicks / impressions * 100) if impressions else 0.0,
        "cpc": (spend / clicks) if clicks else 0.0,
        "cpm": (spend / impressions * 1000) if impressions else 0.0,
        "cpa": (spend / conversions) if conversions else 0.0,
        "revenue": revenue,
        "pb_clicks": pb_clicks,
        "cvr": (pb_conversions / pb_clicks * 100) if pb_clicks else 0.0,
        "profit": revenue - spend,
    }
    active = sum(1 for row in rows if row["r"].operation_status == "ENABLE")

    # account dropdown: only accounts that actually have campaigns cached
    adv_ids_with_campaigns = {r.advertiser_id for r in records}
    account_options = sorted(
        ((aid, (accounts[aid].advertiser_name or aid) if aid in accounts else aid)
         for aid in adv_ids_with_campaigns),
        key=lambda t: t[1].lower())

    # --- entity linking: creative / spark / BC per campaign ---------------------
    shown_ids = [row["r"].campaign_id for row in rows]
    creative_by_cid: dict[str, models.Creative] = {}
    if shown_ids:
        for c in (db.query(models.Creative)
                  .filter(models.Creative.used_campaign_id.in_(shown_ids))):
            creative_by_cid.setdefault(c.used_campaign_id, c)
    spark_by_cid: dict[str, str] = {}
    if shown_ids:
        spark_names = {s.id: (s.name or "") for s in db.query(models.SparkCode).all()}
        for log_row in (db.query(models.LaunchLog)
                        .filter(models.LaunchLog.campaign_id.in_(shown_ids),
                                models.LaunchLog.spark_code_id != None)):        # noqa: E711
            spark_by_cid.setdefault(log_row.campaign_id, spark_names.get(log_row.spark_code_id, ""))
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    bc_by_aid = {aid: bc_names.get(a.owner_bc_id, "") for aid, a in accounts.items() if a.owner_bc_id}
    source_options = sorted({s for s in sources.values() if s})

    # --- KPI period-over-period deltas + sparklines (DB-only, no API cost) -----
    # Previous window = equal-length span immediately before the current one.
    # Basis is the SAME campaign/source set as the visible rows so the delta is
    # consistent with each tile's headline; only shown when a prior figure exists.
    import json as _json2

    from sqlalchemy import func as _func
    shown_cids = [row["r"].campaign_id for row in rows]
    shown_srcs = {row["source"] for row in rows if row["source"]}
    span = end_utc - start_utc
    prev_start, prev_end = start_utc - span, start_utc
    prev_spend = 0.0
    if shown_cids:
        ps_day = timeutil.local_date_str(prev_start)
        pe_day = timeutil.local_date_str(prev_end - _td(seconds=1))
        prev_spend = float(
            db.query(_func.coalesce(_func.sum(models.SpendSnapshot.spend), 0.0))
            .filter(models.SpendSnapshot.campaign_id.in_(shown_cids),
                    models.SpendSnapshot.day >= ps_day,
                    models.SpendSnapshot.day <= pe_day).scalar() or 0)
    prev_pb = pnl_data.revenue_by_source(db, prev_start, prev_end)
    prev_rev = sum(prev_pb.get(s, {}).get("revenue", 0.0) for s in shown_srcs)
    prev = {"spend": prev_spend, "revenue": prev_rev,
            "profit": prev_rev - prev_spend,
            "roas": (prev_rev / prev_spend) if prev_spend else 0.0}

    def _pct(cur, was):
        return ((cur - was) / was * 100) if was else None
    cur_roas = (totals["revenue"] / totals["spend"]) if totals["spend"] else 0.0
    deltas = {
        "has_prev": bool(prev_spend > 0 or prev_rev > 0),
        "spend_pct": _pct(totals["spend"], prev["spend"]),
        "revenue_pct": _pct(totals["revenue"], prev["revenue"]),
        "profit_abs": totals["profit"] - prev["profit"],   # $ change (signed base)
        "roas_abs": (cur_roas - prev["roas"]) if prev["spend"] else None,
    }

    # sparklines: real per-day series, only when the range is wide enough to read
    spark = {}
    days_in_range = round(span.total_seconds() / 86400)
    if days_in_range >= 3 and shown_cids:
        _days, _sp, _rv = pnl_data.daily_series(
            db, start_utc, end_utc, shown_cids, shown_srcs)
        spark = {"spend": _sp, "revenue": _rv}

    # delivery pace (today only — history ticks are cumulative today-values)
    pace_by_cid: dict = {}
    if range_key == "today" and rows:
        from .. import pace as _pace
        pace_by_cid = _pace.compute(db, {row["r"].campaign_id: row["m"] for row in rows
                                         if row["r"].operation_status == "ENABLE"})
    pace_tot = {"impressions": 0, "clicks": 0, "spend": 0.0, "n": 0}
    for p in pace_by_cid.values():
        w = p["w"].get(15)
        if w and w["impressions"] is not None:
            pace_tot["impressions"] += w["impressions"]; pace_tot["clicks"] += w["clicks"]
            pace_tot["spend"] += w["spend"]; pace_tot["n"] += 1

    return render(request, "status.html", {
        "pace": pace_by_cid, "pace_tot": pace_tot,
        "deltas": deltas, "spark_json": _json2.dumps(spark),
        "creative_by_cid": creative_by_cid, "spark_by_cid": spark_by_cid,
        "bc_by_aid": bc_by_aid, "source_f": source_f, "source_options": source_options,
        "rows": rows, "totals": totals, "active_count": active,
        "synced_ago": queries.campaigns_synced_ago(db),
        "q": q, "state": state, "account": account, "sort": sort, "origin": origin,
        "range_key": range_key, "start": start or "", "end": end or "",
        "range_errors": range_errors,
        "pending_tool": pending_tool, "pending_details": pending_details,
        "account_options": account_options,
        "title": "Campaigns",
    })


@router.post("/status/verify-pending")
def verify_pending(db: Session = Depends(get_db)):
    """For every tool-launched campaign missing from the cache, query TikTok BY
    CAMPAIGN ID (including deleted status) and report exactly what it says."""
    import json as _json

    from .. import tiktok_api
    cached = {r.campaign_id for r in db.query(models.CampaignRecord.campaign_id)}
    tool_logs = (db.query(models.LaunchLog)
                 .filter(models.LaunchLog.ok == True,          # noqa: E712
                         models.LaunchLog.campaign_id != "").all())
    pending = [l for l in tool_logs if l.campaign_id not in cached]
    if not pending:
        return RedirectResponse("/status?ok=nothing+pending+to+verify", status_code=303)
    results = []
    for log in pending[:10]:
        acct = (db.query(models.AdAccount)
                .filter_by(advertiser_id=log.advertiser_id).first())
        if not acct or not acct.access_token:
            results.append(f"{log.campaign_id}: account not connected")
            continue
        try:
            data = tiktok_api.list_campaigns(
                acct.access_token, acct.advertiser_id,
                filtering={"campaign_ids": [log.campaign_id]})
            found = data.get("list", [])
            if not found:
                # not in the default listing — is it DELETED?
                data2 = tiktok_api.list_campaigns(
                    acct.access_token, acct.advertiser_id,
                    filtering={"campaign_ids": [log.campaign_id],
                               "secondary_status": "CAMPAIGN_STATUS_DELETE"})
                if data2.get("list", []):
                    results.append(f"{log.campaign_id}: DELETED on TikTok (removed in Ads Manager)")
                    # stop counting it as pending — clear the stale link
                    log.campaign_id = f"deleted:{log.campaign_id}"
                    db.commit()
                else:
                    results.append(f"{log.campaign_id}: NOT FOUND on account {log.advertiser_id}")
            else:
                c = found[0]
                # found but missing from cache → self-heal: insert it now
                if not (db.query(models.CampaignRecord)
                        .filter_by(campaign_id=log.campaign_id).first()):
                    db.add(models.CampaignRecord(
                        advertiser_id=log.advertiser_id,
                        campaign_id=log.campaign_id,
                        campaign_name=c.get("campaign_name", ""),
                        objective_type=c.get("objective_type", ""),
                        operation_status=c.get("operation_status", ""),
                        secondary_status=c.get("secondary_status", ""),
                        budget=float(c.get("budget", 0) or 0),
                        budget_mode=c.get("budget_mode", "")))
                    db.commit()
                results.append(
                    f"{log.campaign_id}: EXISTS ({c.get('operation_status')}, "
                    f"{c.get('secondary_status', '?')}) — added to the list")
        except tiktok_api.TikTokError as e:
            results.append(f"{log.campaign_id}: lookup failed, code {e.code} {e.message[:60]}")
    msg = " · ".join(results)[:400].replace(" ", "+")
    return RedirectResponse(f"/status?ok={msg}", status_code=303)


@router.post("/status/sync")
def sync_now(request: Request, db: Session = Depends(get_db)):
    """Queue a full campaign sync; the page refreshes itself when the job's
    notification arrives. Returns JSON to fetch() callers, a redirect otherwise."""
    from .. import jobs
    from fastapi.responses import JSONResponse
    running = (db.query(models.Job).filter(models.Job.kind == "status_sync",
                                           models.Job.status.in_(("queued", "running"))).count())
    if running:
        job_id = None
    else:
        job_id = jobs.enqueue(db, "status_sync", "Sync campaigns from TikTok", {}, href="/status").id
    if request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"queued": True, "job_id": job_id, "already": bool(running)})
    return RedirectResponse("/status?ok=Syncing+in+the+background+—+you%27ll+get+a+notification.", status_code=303)
