"""Delivery pace — how fast impressions, clicks and spend are moving RIGHT NOW,
per campaign, from the MetricTick rows the campaign sync writes every sweep.

Why: a bid change only shows in delivery velocity, not in today's totals. A
row saying "22 impressions" tells you nothing; "+9 in the last 15 min, +2 in
the last 5" tells you whether the auction is picking the ad up.

The numbers TikTok reports are cumulative for the day, so a delta is
current − the tick just before the window start. At local midnight the totals
reset; a negative delta means exactly that and is clamped to 0."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models

PACE_KEEP_HOURS = 6
WINDOWS = (5, 15, 60)            # minutes
BUCKET_MIN = 5                   # micro-bar resolution
BUCKETS = 12                     # 12 × 5 min = the last hour


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record(db: Session, records: list, now: datetime | None = None) -> int:
    """Write one tick per ACTIVE campaign from freshly synced CampaignRecords.
    Called by live_spend.sync_campaigns; commits with the caller."""
    now = now or _now()
    n = 0
    for r in records:
        if r.operation_status != "ENABLE":
            continue
        db.add(models.MetricTick(
            advertiser_id=r.advertiser_id, campaign_id=r.campaign_id, at=now,
            spend=float(r.spend_today or 0), impressions=int(r.impressions or 0),
            clicks=int(r.clicks or 0), conversions=int(r.conversions or 0)))
        n += 1
    return n


def prune(db: Session, now: datetime | None = None) -> int:
    cutoff = (now or _now()) - timedelta(hours=PACE_KEEP_HOURS)
    n = db.query(models.MetricTick).filter(models.MetricTick.at < cutoff).delete()
    return n


def _delta(cur: dict, base: models.MetricTick | None) -> dict:
    if base is None:
        return {"impressions": None, "clicks": None, "spend": None}
    return {
        "impressions": max(cur["impressions"] - (base.impressions or 0), 0),
        "clicks": max(cur["clicks"] - (base.clicks or 0), 0),
        "spend": max(cur["spend"] - float(base.spend or 0), 0.0),
    }


def compute(db: Session, current: dict[str, dict], now: datetime | None = None) -> dict[str, dict]:
    """current: {campaign_id: {spend, impressions, clicks}} as shown on the page.
    Returns {campaign_id: {"w": {5: {impressions, clicks, spend}|None…},
                           "bars": [impressions per 5-min bucket, oldest→newest],
                           "since_min": minutes of history available,
                           "live": True when something moved in the last 5 min}}.
    A window whose start predates the oldest tick is reported against the
    oldest tick and flagged partial via since_min."""
    now = now or _now()
    ids = list(current)
    if not ids:
        return {}
    oldest = now - timedelta(minutes=max(WINDOWS) + BUCKET_MIN)
    ticks = (db.query(models.MetricTick)
             .filter(models.MetricTick.campaign_id.in_(ids), models.MetricTick.at >= oldest)
             .order_by(models.MetricTick.campaign_id, models.MetricTick.at).all())
    by_cid: dict[str, list[models.MetricTick]] = {}
    for t in ticks:
        by_cid.setdefault(t.campaign_id, []).append(t)
    out: dict[str, dict] = {}
    for cid, cur in current.items():
        hist = by_cid.get(cid) or []
        if not hist:
            out[cid] = {"w": {w: None for w in WINDOWS}, "bars": [0] * BUCKETS, "since_min": 0, "live": False}
            continue

        def tick_before(t: datetime) -> models.MetricTick | None:
            best = None
            for h in hist:
                if h.at <= t:
                    best = h
                else:
                    break
            return best or hist[0]      # window older than history → measure from the oldest tick

        w = {}
        for mins in WINDOWS:
            base = tick_before(now - timedelta(minutes=mins))
            w[mins] = _delta(cur, base)
        bars = []
        for i in range(BUCKETS, 0, -1):
            b0 = tick_before(now - timedelta(minutes=i * BUCKET_MIN))
            b1 = tick_before(now - timedelta(minutes=(i - 1) * BUCKET_MIN)) if i > 1 else None
            end_val = cur["impressions"] if b1 is None else (b1.impressions or 0)
            bars.append(max(end_val - (b0.impressions or 0), 0))
        since = int((now - hist[0].at).total_seconds() // 60)
        out[cid] = {"w": w, "bars": bars, "since_min": since,
                    "live": bool(w[5]["impressions"] or w[5]["clicks"])}
    return out
