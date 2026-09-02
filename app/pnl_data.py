"""Shared P&L computation — used by the profit rules, Overview KPIs and P&L page.

Revenue truth: PostbackEvent rows (Glitchy). Spend truth: SpendSnapshot rows,
joined to sources through LaunchLog (campaign -> source).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, timeutil


def campaign_source_map(db: Session) -> dict[str, str]:
    """campaign_id -> source, from successful launches that carried a source."""
    out = {}
    for log in (db.query(models.LaunchLog)
                .filter(models.LaunchLog.ok == True,          # noqa: E712
                        models.LaunchLog.source != "")):
        if log.campaign_id:
            out[log.campaign_id] = log.source
    return out


def spend_by_source(db: Session, start_utc: datetime, end_utc: datetime) -> dict[str, float]:
    camp_source = campaign_source_map(db)
    if not camp_source:
        return {}
    start_day = timeutil.local_date_str(start_utc)
    end_day = timeutil.local_date_str(end_utc)
    out: dict[str, float] = {}
    rows = (db.query(models.SpendSnapshot)
            .filter(models.SpendSnapshot.campaign_id.in_(list(camp_source)),
                    models.SpendSnapshot.day >= start_day,
                    models.SpendSnapshot.day <= end_day).all())
    for r in rows:
        src = camp_source[r.campaign_id]
        out[src] = out.get(src, 0.0) + float(r.spend or 0)
    return out


def revenue_by_source(db: Session, start_utc: datetime, end_utc: datetime) -> dict[str, dict]:
    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)
    out = {}
    rows = (db.query(models.PostbackEvent.source,
                     func.sum(models.PostbackEvent.revenue).label("revenue"),
                     func.sum(models.PostbackEvent.conversions).label("conversions"),
                     func.sum(models.PostbackEvent.clicks).label("clicks"))
            .filter(models.PostbackEvent.created_at >= s_naive,
                    models.PostbackEvent.created_at < e_naive)
            .group_by(models.PostbackEvent.source).all())
    for r in rows:
        out[r.source] = {"revenue": float(r.revenue or 0),
                         "conversions": int(r.conversions or 0),
                         "clicks": int(r.clicks or 0)}
    return out


def source_pnl(db: Session, start_utc: datetime, end_utc: datetime) -> dict[str, dict]:
    """source -> {revenue, spend, profit, clicks, conversions}."""
    spend = spend_by_source(db, start_utc, end_utc)
    revenue = revenue_by_source(db, start_utc, end_utc)
    out: dict[str, dict] = {}
    for src in set(spend) | set(revenue):
        rev = revenue.get(src, {})
        row = {"revenue": rev.get("revenue", 0.0),
               "clicks": rev.get("clicks", 0),
               "conversions": rev.get("conversions", 0),
               "spend": spend.get(src, 0.0)}
        row["profit"] = row["revenue"] - row["spend"]
        out[src] = row
    return out


def daily_series(db: Session, start_utc: datetime, end_utc: datetime,
                 campaign_ids: list[str] | None = None,
                 sources: set[str] | None = None) -> tuple[list[str], list[float], list[float]]:
    """Per-local-day (spend, revenue) over [start,end), restricted to a campaign
    set (spend) and a source set (revenue). DB-only — for KPI sparklines.
    Returns (days, spend_per_day, revenue_per_day) aligned by index."""
    from datetime import timedelta
    # ordered list of local day strings the range covers
    days: list[str] = []
    cur = start_utc
    while cur < end_utc:
        days.append(timeutil.local_date_str(cur))
        cur += timedelta(days=1)
    days = sorted(set(days))
    idx = {d: i for i, d in enumerate(days)}
    spend = [0.0] * len(days)
    rev = [0.0] * len(days)

    sq = db.query(models.SpendSnapshot).filter(
        models.SpendSnapshot.day >= days[0], models.SpendSnapshot.day <= days[-1])
    if campaign_ids:
        sq = sq.filter(models.SpendSnapshot.campaign_id.in_(list(campaign_ids)))
    for r in sq.all():
        if r.day in idx:
            spend[idx[r.day]] += float(r.spend or 0)

    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)
    pq = db.query(models.PostbackEvent).filter(
        models.PostbackEvent.created_at >= s_naive,
        models.PostbackEvent.created_at < e_naive)
    for p in pq.all():
        if sources is not None and p.source not in sources:
            continue
        d = timeutil.local_date_str(p.created_at)
        if d in idx:
            rev[idx[d]] += float(p.revenue or 0)
    return days, spend, rev


def overall_totals(db: Session, start_utc: datetime, end_utc: datetime) -> dict:
    """Range KPIs for the Overview: spend is ALL spend (snapshots, sourced or
    not); revenue/clicks/conversions from all postbacks."""
    start_day = timeutil.local_date_str(start_utc)
    end_day = timeutil.local_date_str(end_utc)
    spend = float(db.query(func.coalesce(func.sum(models.SpendSnapshot.spend), 0.0))
                  .filter(models.SpendSnapshot.day >= start_day,
                          models.SpendSnapshot.day <= end_day).scalar() or 0)
    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)
    rev, conv, clicks = (db.query(
        func.coalesce(func.sum(models.PostbackEvent.revenue), 0.0),
        func.coalesce(func.sum(models.PostbackEvent.conversions), 0),
        func.coalesce(func.sum(models.PostbackEvent.clicks), 0))
        .filter(models.PostbackEvent.created_at >= s_naive,
                models.PostbackEvent.created_at < e_naive).one())
    revenue, conversions, clicks = float(rev or 0), int(conv or 0), int(clicks or 0)
    return {
        "spend": spend, "revenue": revenue, "profit": revenue - spend,
        "roas": (revenue / spend) if spend else 0.0,
        "clicks": clicks, "conversions": conversions,
        "cvr": (conversions / clicks * 100) if clicks else 0.0,
        "cpa": (spend / conversions) if conversions else 0.0,
    }
