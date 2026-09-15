"""Shared P&L computation — used by the profit rules, Overview KPIs and P&L page.

Revenue truth: PostbackEvent rows (Glitchy). Spend truth: SpendSnapshot rows,
joined to sources through LaunchLog (campaign -> source).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, timeutil


def campaign_source_map(db: Session, advertiser_ids=None) -> dict[str, str]:
    """campaign_id -> source, from successful launches that carried a source.
    `advertiser_ids` (a set) restricts it to those accounts — a user's view."""
    out = {}
    q = (db.query(models.LaunchLog.campaign_id, models.LaunchLog.source, models.LaunchLog.advertiser_id)
         .filter(models.LaunchLog.ok == True,          # noqa: E712
                 models.LaunchLog.source != ""))
    if advertiser_ids is not None:
        if not advertiser_ids:
            return {}
        q = q.filter(models.LaunchLog.advertiser_id.in_(list(advertiser_ids)))
    for cid, src, _adv in q:
        if cid:
            out[cid] = src
    return out


def source_weights(db: Session, start_utc: datetime, end_utc: datetime, advertiser_ids) -> dict[str, float]:
    """source -> the share of that source's revenue that belongs to these accounts, for
    a per-user view. A source whose campaigns all run on the user's accounts weighs 1;
    one shared with another user is split by each side's spend in the range (even split
    when nothing spent yet); a source with none of the user's campaigns is absent.
    Campaigns matched by name (built in Ads Manager) count like launched ones."""
    if advertiser_ids is None:
        return {}
    ids = set(advertiser_ids)
    camp_src = campaign_source_map(db)
    camp_adv = {cid: adv for cid, adv in db.query(models.CampaignRecord.campaign_id, models.CampaignRecord.advertiser_id)}
    for cid, adv in db.query(models.LaunchLog.campaign_id, models.LaunchLog.advertiser_id).filter(models.LaunchLog.ok == True):   # noqa: E712
        camp_adv.setdefault(cid, adv)
    # sources with no launch of their own: campaigns whose TikTok name is the source
    src_seen = {r[0] for r in db.query(models.PostbackEvent.source).distinct().all()}
    for src, cids in campaigns_named(db, [s for s in src_seen if s]).items():
        for cid in cids:
            camp_src.setdefault(cid, src)
    if not camp_src:
        return {}
    start_day = timeutil.local_date_str(start_utc)
    end_day = timeutil.local_date_str(end_utc - timedelta(seconds=1))
    spend: dict[str, float] = {}
    for cid, sp in (db.query(models.SpendSnapshot.campaign_id, func.sum(models.SpendSnapshot.spend))
                    .filter(models.SpendSnapshot.campaign_id.in_(list(camp_src)),
                            models.SpendSnapshot.day >= start_day, models.SpendSnapshot.day <= end_day)
                    .group_by(models.SpendSnapshot.campaign_id)):
        spend[cid] = float(sp or 0)
    mine_spend: dict[str, float] = {}
    all_spend: dict[str, float] = {}
    mine_n: dict[str, int] = {}
    all_n: dict[str, int] = {}
    for cid, src in camp_src.items():
        sp = spend.get(cid, 0.0)
        all_spend[src] = all_spend.get(src, 0.0) + sp
        all_n[src] = all_n.get(src, 0) + 1
        if camp_adv.get(cid) in ids:
            mine_spend[src] = mine_spend.get(src, 0.0) + sp
            mine_n[src] = mine_n.get(src, 0) + 1
    out: dict[str, float] = {}
    for src, n in mine_n.items():
        if n == all_n[src]:
            out[src] = 1.0
        elif all_spend[src] > 0:
            out[src] = mine_spend[src] / all_spend[src]
        else:
            out[src] = n / all_n[src]
    return out


def campaigns_named(db: Session, sources) -> dict[str, list[str]]:
    """source -> campaign ids whose TikTok campaign NAME is that source, for sources this
    dashboard never launched (a campaign built by hand in Ads Manager with
    &source=__CAMPAIGN_NAME__ on its URL). Only the given sources are looked up, so a
    synced campaign never becomes a P&L row just by existing."""
    wanted = {s for s in sources if s}
    if not wanted:
        return {}
    out: dict[str, list[str]] = {}
    for cid, name in (db.query(models.CampaignRecord.campaign_id, models.CampaignRecord.campaign_name)
                      .filter(models.CampaignRecord.campaign_name.in_(list(wanted)))):
        out.setdefault(name, []).append(cid)
    return out


def spend_by_source(db: Session, start_utc: datetime, end_utc: datetime, advertiser_ids=None) -> dict[str, float]:
    camp_source = campaign_source_map(db, advertiser_ids)
    if not camp_source:
        return {}
    start_day = timeutil.local_date_str(start_utc)
    end_day = timeutil.local_date_str(end_utc - timedelta(seconds=1))   # [start, end) — end is the next midnight
    out: dict[str, float] = {}
    rows = (db.query(models.SpendSnapshot)
            .filter(models.SpendSnapshot.campaign_id.in_(list(camp_source)),
                    models.SpendSnapshot.day >= start_day,
                    models.SpendSnapshot.day <= end_day).all())
    for r in rows:
        src = camp_source[r.campaign_id]
        out[src] = out.get(src, 0.0) + float(r.spend or 0)
    return out


def revenue_by_source(db: Session, start_utc: datetime, end_utc: datetime, advertiser_ids=None) -> dict[str, dict]:
    """source -> {revenue, conversions, clicks}. With `advertiser_ids`, only the sources
    that run on those accounts, each weighted by the user's share (source_weights)."""
    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)
    weights = source_weights(db, start_utc, end_utc, advertiser_ids) if advertiser_ids is not None else None
    out = {}
    rows = (db.query(models.PostbackEvent.source,
                     func.sum(models.PostbackEvent.revenue).label("revenue"),
                     func.sum(models.PostbackEvent.conversions).label("conversions"),
                     func.sum(models.PostbackEvent.clicks).label("clicks"))
            .filter(models.PostbackEvent.created_at >= s_naive,
                    models.PostbackEvent.created_at < e_naive)
            .group_by(models.PostbackEvent.source).all())
    for r in rows:
        w = 1.0
        if weights is not None:
            w = weights.get(r.source, 0.0)
            if w <= 0:
                continue
        out[r.source] = {"revenue": float(r.revenue or 0) * w,
                         "conversions": int(round(int(r.conversions or 0) * w)),
                         "clicks": int(round(int(r.clicks or 0) * w)),
                         **({"shared": True} if w < 1.0 else {})}
    return out


def source_pnl(db: Session, start_utc: datetime, end_utc: datetime, advertiser_ids=None) -> dict[str, dict]:
    """source -> {revenue, spend, profit, clicks, conversions}."""
    spend = spend_by_source(db, start_utc, end_utc, advertiser_ids)
    revenue = revenue_by_source(db, start_utc, end_utc, advertiser_ids)
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
                 sources: set[str] | None = None,
                 weights: dict[str, float] | None = None) -> tuple[list[str], list[float], list[float]]:
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
        w = weights.get(p.source, 0.0) if weights is not None else 1.0
        if w <= 0:
            continue
        d = timeutil.local_date_str(p.created_at)
        if d in idx:
            rev[idx[d]] += float(p.revenue or 0) * w
    return days, spend, rev


def overall_totals(db: Session, start_utc: datetime, end_utc: datetime, advertiser_ids=None) -> dict:
    """Range KPIs for the Overview: spend is ALL spend (snapshots, sourced or
    not); revenue/clicks/conversions from all postbacks. With `advertiser_ids` (a
    user's view): that view's spend, and its weighted share of the sources it runs."""
    start_day = timeutil.local_date_str(start_utc)
    end_day = timeutil.local_date_str(end_utc - timedelta(seconds=1))   # [start, end) — end is the next midnight
    sq = (db.query(func.coalesce(func.sum(models.SpendSnapshot.spend), 0.0))
          .filter(models.SpendSnapshot.day >= start_day, models.SpendSnapshot.day <= end_day))
    if advertiser_ids is not None:
        sq = sq.filter(models.SpendSnapshot.advertiser_id.in_(list(advertiser_ids) or [""]))
    spend = float(sq.scalar() or 0)
    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)
    if advertiser_ids is None:
        rev, conv, clicks = (db.query(
            func.coalesce(func.sum(models.PostbackEvent.revenue), 0.0),
            func.coalesce(func.sum(models.PostbackEvent.conversions), 0),
            func.coalesce(func.sum(models.PostbackEvent.clicks), 0))
            .filter(models.PostbackEvent.created_at >= s_naive,
                    models.PostbackEvent.created_at < e_naive).one())
        revenue, conversions, clicks = float(rev or 0), int(conv or 0), int(clicks or 0)
    else:
        by_src = revenue_by_source(db, start_utc, end_utc, advertiser_ids)
        revenue = sum(r["revenue"] for r in by_src.values())
        conversions = sum(r["conversions"] for r in by_src.values())
        clicks = sum(r["clicks"] for r in by_src.values())
    return {
        "spend": spend, "revenue": revenue, "profit": revenue - spend,
        "roas": (revenue / spend) if spend else 0.0,
        "clicks": clicks, "conversions": conversions,
        "cvr": (conversions / clicks * 100) if clicks else 0.0,
        "cpa": (spend / conversions) if conversions else 0.0,
    }
