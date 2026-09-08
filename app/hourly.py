"""Hourly rollups — TikTok reports cumulative today-values; every sync writes
the latest cumulative figure into the current LOCAL hour's bucket. Reading the
buckets back as consecutive differences gives spend / clicks / conversions per
hour, for today and for the days before (kept 8 days), without any API call.

Revenue per hour comes straight from PostbackEvent.created_at."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, timeutil

KEEP_DAYS = 8
FIELDS = ("spend", "impressions", "clicks", "conversions")


def _local(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timeutil.TZ)


def record(db: Session, records: list, now: datetime | None = None) -> int:
    """Upsert this hour's bucket for every ACTIVE campaign in `records`
    (fresh CampaignRecords). Commits with the caller."""
    loc = _local(now)
    day, hour = loc.strftime("%Y-%m-%d"), loc.hour
    ids = [r.campaign_id for r in records if r.operation_status == "ENABLE"]
    if not ids:
        return 0
    have = {h.campaign_id: h for h in db.query(models.HourlyMetric)
            .filter(models.HourlyMetric.day == day, models.HourlyMetric.hour == hour,
                    models.HourlyMetric.campaign_id.in_(ids))}
    n = 0
    for r in records:
        if r.operation_status != "ENABLE":
            continue
        vals = {"spend": float(r.spend_today or 0), "impressions": int(r.impressions or 0),
                "clicks": int(r.clicks or 0), "conversions": int(r.conversions or 0)}
        h = have.get(r.campaign_id)
        if h is None:
            h = models.HourlyMetric(advertiser_id=r.advertiser_id, campaign_id=r.campaign_id, day=day, hour=hour)
            db.add(h)
        for k, v in vals.items():
            # cumulative for the day → never let a stale/partial read lower the bucket
            if v >= (getattr(h, k) or 0):
                setattr(h, k, v)
        h.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        n += 1
    return n


def prune(db: Session, now: datetime | None = None) -> int:
    cutoff = (_local(now) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    return db.query(models.HourlyMetric).filter(models.HourlyMetric.day < cutoff).delete(synchronize_session=False)


def series(db: Session, campaign_ids: list[str], day: str) -> dict[str, list[float]]:
    """{field: [24 values]} — per-hour DELTAS summed over the campaigns for one
    local day. Hours without a bucket inherit the previous hour's cumulative
    figure (no delivery), so a gap never shows as a spike."""
    out = {k: [0.0] * 24 for k in FIELDS}
    if not campaign_ids:
        return out
    rows = (db.query(models.HourlyMetric)
            .filter(models.HourlyMetric.day == day, models.HourlyMetric.campaign_id.in_(campaign_ids))
            .order_by(models.HourlyMetric.campaign_id, models.HourlyMetric.hour).all())
    by_c: dict[str, dict[int, models.HourlyMetric]] = {}
    for r in rows:
        by_c.setdefault(r.campaign_id, {})[r.hour] = r
    for cid, hours in by_c.items():
        prev = {k: 0.0 for k in FIELDS}
        for h in range(24):
            r = hours.get(h)
            if r is None:
                continue
            for k in FIELDS:
                cur = float(getattr(r, k) or 0)
                out[k][h] += max(cur - prev[k], 0.0)
                prev[k] = max(prev[k], cur)
    return out


def series_by_campaign(db: Session, campaign_ids: list[str], day: str, field: str = "spend") -> dict[str, list[float]]:
    """{campaign_id: [24 deltas]} for one field — the per-row 'last 12h' bars."""
    out: dict[str, list[float]] = {}
    if not campaign_ids:
        return out
    rows = (db.query(models.HourlyMetric)
            .filter(models.HourlyMetric.day == day, models.HourlyMetric.campaign_id.in_(campaign_ids))
            .order_by(models.HourlyMetric.campaign_id, models.HourlyMetric.hour).all())
    by_c: dict[str, dict[int, float]] = {}
    for r in rows:
        by_c.setdefault(r.campaign_id, {})[r.hour] = float(getattr(r, field) or 0)
    for cid, hours in by_c.items():
        vals, prev = [0.0] * 24, 0.0
        for h in range(24):
            if h in hours:
                vals[h] = max(hours[h] - prev, 0.0)
                prev = max(prev, hours[h])
        out[cid] = vals
    return out


def revenue_series(db: Session, sources, day: str) -> list[float]:
    """[24 values] postback revenue per local hour for these sources
    (None = every postback)."""
    out = [0.0] * 24
    if sources is not None and not sources:
        return out
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timeutil.TZ)
    end = start + timedelta(days=1)
    s_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
    e_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
    q = (db.query(models.PostbackEvent.source, models.PostbackEvent.revenue, models.PostbackEvent.created_at)
         .filter(models.PostbackEvent.created_at >= s_utc, models.PostbackEvent.created_at < e_utc))
    if sources is not None:
        q = q.filter(models.PostbackEvent.source.in_(list(sources)))
    for src, rev, at in q:
        if at is None:
            continue
        loc = at.replace(tzinfo=timezone.utc).astimezone(timeutil.TZ)
        out[loc.hour] += float(rev or 0)
    return out


def days_available(db: Session) -> int:
    return db.query(func.count(func.distinct(models.HourlyMetric.day))).scalar() or 0
