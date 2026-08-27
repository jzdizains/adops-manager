"""Live spend/metric pulls from TikTok reporting, plus the campaign-cache sync
the Status page reads ('synced X ago')."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from . import models, queries, tiktok_api, timeutil


METRICS = ["spend", "impressions", "clicks", "conversion", "cpc", "cpm", "ctr"]
REPORT_METRICS = METRICS + ["cost_per_conversion"]


def _f(m: dict, key: str) -> float:
    try:
        return float(m.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def sync_campaigns(db: Session, accounts: list[models.AdAccount] | None = None) -> dict:
    """Pull campaign lists + today's spend for every enabled account into
    CampaignRecord rows. Returns {synced, errors}."""
    import time as _time
    accounts = accounts or queries.enabled_accounts(db)
    today = timeutil.local_date_str()
    synced, errors = 0, []
    for acct in accounts:
        if not acct.access_token:
            continue
        if synced:
            _time.sleep(0.15)   # spread calls — rate-limit safety at scale
        try:
            data = tiktok_api.list_campaigns(acct.access_token, acct.advertiser_id)
            campaigns = data.get("list", [])
            for c in campaigns:
                c["_smart_plus"] = False
            # Smart+ campaigns live behind their own endpoint — merge them in.
            # Accounts without Smart+ access just error/return empty: ignore.
            try:
                spc = tiktok_api.smart_plus_campaign_get(acct.access_token, acct.advertiser_id)
                known = {str(c.get("campaign_id", "")) for c in campaigns}
                for c in spc.get("list", []):
                    if str(c.get("campaign_id", "")) not in known:
                        c["_smart_plus"] = True
                        campaigns.append(c)
            except tiktok_api.TikTokError:
                pass
            metrics_by_campaign: dict[str, dict] = {}
            try:
                rows = tiktok_api.get_report(
                    acct.access_token, acct.advertiser_id,
                    dimensions=["campaign_id"], metrics=REPORT_METRICS,
                    start_date=today, end_date=today)
                for r in rows:
                    cid = str(r.get("dimensions", {}).get("campaign_id", ""))
                    metrics_by_campaign[cid] = r.get("metrics", {}) or {}
            except tiktok_api.TikTokError:
                pass  # reporting can lag; keep the campaign list anyway

            db.query(models.CampaignRecord).filter_by(advertiser_id=acct.advertiser_id).delete()
            for c in campaigns:
                cid = str(c.get("campaign_id", ""))
                m = metrics_by_campaign.get(cid, {})
                launched_at = None
                raw_created = str(c.get("create_time", "") or "")
                if raw_created:
                    try:  # TikTok sends "YYYY-MM-DD HH:MM:SS" (UTC)
                        launched_at = datetime.strptime(raw_created[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pass
                db.add(models.CampaignRecord(
                    advertiser_id=acct.advertiser_id,
                    campaign_id=cid,
                    campaign_name=c.get("campaign_name", ""),
                    objective_type=c.get("objective_type", ""),
                    operation_status=c.get("operation_status", ""),
                    secondary_status=c.get("secondary_status", ""),
                    budget=float(c.get("budget", 0) or 0),
                    budget_mode=c.get("budget_mode", ""),
                    spend_today=_f(m, "spend"),
                    impressions=int(_f(m, "impressions")),
                    clicks=int(_f(m, "clicks")),
                    conversions=int(_f(m, "conversion")),
                    cpm=_f(m, "cpm"),
                    cpc=_f(m, "cpc"),
                    cpa=_f(m, "cost_per_conversion"),
                    ctr=_f(m, "ctr"),
                    launched_at=launched_at,
                    is_smart_plus=bool(c.get("_smart_plus")),
                ))
                # upsert today's spend snapshot (P&L history)
                snap = (db.query(models.SpendSnapshot)
                        .filter_by(campaign_id=cid, day=today).first())
                if not snap:
                    snap = models.SpendSnapshot(
                        advertiser_id=acct.advertiser_id, campaign_id=cid, day=today)
                    db.add(snap)
                snap.spend = _f(m, "spend")
                snap.conversions = int(_f(m, "conversion"))
            db.commit()
            synced += 1
        except tiktok_api.TikTokError as e:
            db.rollback()
            errors.append({"advertiser_id": acct.advertiser_id, "code": str(e.code), "message": e.message})
    return {"synced": synced, "errors": errors}


def account_day_metrics(acct: models.AdAccount, start_date: str, end_date: str) -> dict:
    """Advertiser-level metric totals for a date range."""
    rows = tiktok_api.get_report(
        acct.access_token, acct.advertiser_id,
        dimensions=["advertiser_id"], metrics=METRICS,
        data_level="AUCTION_ADVERTISER",
        start_date=start_date, end_date=end_date)
    out = {m: 0.0 for m in METRICS}
    for r in rows:
        for m in METRICS:
            out[m] += float(r.get("metrics", {}).get(m, 0) or 0)
    return out
