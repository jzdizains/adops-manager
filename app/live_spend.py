"""Live spend/metric pulls from TikTok reporting, plus the campaign-cache sync
the Status page reads ('synced X ago')."""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import models, queries, tiktok_api, timeutil


METRICS = ["spend", "impressions", "clicks", "conversion", "cpc", "cpm", "ctr"]


def sync_campaigns(db: Session, accounts: list[models.AdAccount] | None = None) -> dict:
    """Pull campaign lists + today's spend for every enabled account into
    CampaignRecord rows. Returns {synced, errors}."""
    accounts = accounts or queries.enabled_accounts(db)
    today = timeutil.local_date_str()
    synced, errors = 0, []
    for acct in accounts:
        if not acct.access_token:
            continue
        try:
            data = tiktok_api.list_campaigns(acct.access_token, acct.advertiser_id)
            campaigns = data.get("list", [])
            spend_by_campaign: dict[str, float] = {}
            try:
                rows = tiktok_api.get_report(
                    acct.access_token, acct.advertiser_id,
                    dimensions=["campaign_id"], metrics=["spend"],
                    start_date=today, end_date=today)
                for r in rows:
                    cid = str(r.get("dimensions", {}).get("campaign_id", ""))
                    spend_by_campaign[cid] = float(r.get("metrics", {}).get("spend", 0) or 0)
            except tiktok_api.TikTokError:
                pass  # reporting can lag; keep the campaign list anyway

            db.query(models.CampaignRecord).filter_by(advertiser_id=acct.advertiser_id).delete()
            for c in campaigns:
                cid = str(c.get("campaign_id", ""))
                db.add(models.CampaignRecord(
                    advertiser_id=acct.advertiser_id,
                    campaign_id=cid,
                    campaign_name=c.get("campaign_name", ""),
                    objective_type=c.get("objective_type", ""),
                    operation_status=c.get("operation_status", ""),
                    secondary_status=c.get("secondary_status", ""),
                    budget=float(c.get("budget", 0) or 0),
                    budget_mode=c.get("budget_mode", ""),
                    spend_today=spend_by_campaign.get(cid, 0.0),
                ))
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
