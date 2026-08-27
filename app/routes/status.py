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
    "spend": lambda row: row["r"].spend_today,
    "impr": lambda row: row["r"].impressions,
    "clicks": lambda row: row["r"].clicks,
    "ctr": lambda row: row["r"].ctr,
    "cpc": lambda row: row["r"].cpc,
    "cpm": lambda row: row["r"].cpm,
    "conv": lambda row: row["r"].conversions,
    "cpa": lambda row: row["r"].cpa,
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
    origin = request.query_params.get("origin", "tool")        # tool | all
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

    # --- Glitchy postback truth for today, per source --------------------------
    start_utc, end_utc = timeutil.range_bounds("today")
    pb = pnl_data.revenue_by_source(db, start_utc, end_utc)

    # how many campaigns share each source + that source's total spend today
    # (computed over ALL records so filters never change the apportioning)
    src_count: dict[str, int] = {}
    src_spend: dict[str, float] = {}
    for r in records:
        src = sources.get(r.campaign_id, "")
        if src:
            src_count[src] = src_count.get(src, 0) + 1
            src_spend[src] = src_spend.get(src, 0.0) + float(r.spend_today or 0)

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

        src = sources.get(r.campaign_id, "")
        src_pb = pb.get(src, {}) if src else {}
        n = src_count.get(src, 1)
        if src and n > 1:
            total = src_spend.get(src, 0.0)
            share = (float(r.spend_today or 0) / total) if total > 0 else (1.0 / n)
        else:
            share = 1.0
        revenue = float(src_pb.get("revenue", 0.0)) * share
        pb_clicks = float(src_pb.get("clicks", 0)) * share
        pb_conv = float(src_pb.get("conversions", 0)) * share
        src_clicks = int(src_pb.get("clicks", 0))
        src_conv = int(src_pb.get("conversions", 0))
        rows.append({
            "r": r, "account_name": name, "source": src,
            "shared_n": n if (src and n > 1) else 0,
            "revenue": revenue,
            "pb_clicks": pb_clicks,
            "pb_conversions": pb_conv,
            # CVR is a ratio → identical for every campaign on the source
            "cvr": (src_conv / src_clicks * 100) if src_clicks else 0.0,
            "profit": revenue - float(r.spend_today or 0),
        })

    reverse = sort not in ("name", "source")
    keyfn = SORT_KEYS[sort]
    rows.sort(key=lambda row: keyfn(row) or (0 if reverse else ""), reverse=reverse)

    # totals across the FILTERED rows; rate metrics recomputed from the sums so
    # they're properly weighted (never an average of averages)
    spend = sum(row["r"].spend_today for row in rows)
    impressions = sum(row["r"].impressions for row in rows)
    clicks = sum(row["r"].clicks for row in rows)
    conversions = sum(row["r"].conversions for row in rows)
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

    return render(request, "status.html", {
        "rows": rows, "totals": totals, "active_count": active,
        "synced_ago": queries.campaigns_synced_ago(db),
        "q": q, "state": state, "account": account, "sort": sort, "origin": origin,
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
def sync_now(db: Session = Depends(get_db)):
    live_spend.sync_campaigns(db)
    return RedirectResponse("/status", status_code=303)
