"""Campaigns board numbers that don't depend on the date range (v151):

  lifetime   — every campaign's spend / revenue / profit / ROAS since it launched, on its row,
               so a campaign that's red today but green over its life reads as such;
  reconcile  — the two lines under the table that make the totals add up: revenue that came in
               on a source no campaign carries ("not matched to any campaign") and spend on
               campaigns this board doesn't show ("outside this board").

Revenue joins campaigns through their source (see status.py); a source shared by several
campaigns is split by each one's share of the source's spend (even split when none spent).
All-time revenue is read once per 5 minutes per process (it scans every postback).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

REV_TTL = 300
_REV: dict = {"at": 0.0, "data": None}


def apportion(cid_spend: dict, cid_src: dict, src_rev: dict) -> dict:
    """{campaign: {spend, revenue, profit, roas, has}} — each campaign's share of its source's
    revenue by spend (even split when the source's campaigns spent nothing). Pure."""
    by_src: dict = {}
    for cid, src in cid_src.items():
        if src:
            by_src.setdefault(src, []).append(cid)
    out = {}
    for cid, spend in cid_spend.items():
        spend = float(spend or 0)
        src = cid_src.get(cid) or ""
        rev = 0.0
        if src:
            peers = by_src.get(src) or [cid]
            tot = sum(float(cid_spend.get(p) or 0) for p in peers)
            share = (spend / tot) if tot > 0 else 1.0 / len(peers)
            rev = float(src_rev.get(src) or 0) * share
        out[cid] = {"spend": round(spend, 2), "revenue": round(rev, 2), "profit": round(rev - spend, 2),
                    "roas": round(rev / spend, 2) if spend and src else 0.0, "has": bool(src)}
    return out


def all_time_revenue(db) -> dict:
    """source → revenue since the first postback (cached REV_TTL)."""
    if _REV["data"] is not None and time.time() - _REV["at"] < REV_TTL:
        return _REV["data"]
    from . import pnl_data
    rows = pnl_data.revenue_by_source(db, datetime(2000, 1, 1, tzinfo=timezone.utc),
                                      datetime.now(timezone.utc) + timedelta(days=1))
    _REV["data"], _REV["at"] = {s: float(v.get("revenue") or 0) for s, v in rows.items()}, time.time()
    return _REV["data"]


def lifetime(db, models, campaign_ids: list[str], sources: dict) -> dict:
    """Lifetime numbers for these campaigns. The source split needs every campaign sharing
    their sources, so those peers' lifetime spend is read too (one grouped query)."""
    from sqlalchemy import func
    ids = [c for c in dict.fromkeys(campaign_ids) if c]
    if not ids:
        return {}
    srcs = {sources.get(c) for c in ids if sources.get(c)}
    peers = [c for c, s in sources.items() if s in srcs]
    want = list(dict.fromkeys(ids + peers))
    spend: dict = {}
    for n in range(0, len(want), 500):
        for cid, s in (db.query(models.SpendSnapshot.campaign_id, func.sum(models.SpendSnapshot.spend))
                       .filter(models.SpendSnapshot.campaign_id.in_(want[n:n + 500]))
                       .group_by(models.SpendSnapshot.campaign_id)):
            spend[cid] = float(s or 0)
    for c in want:
        spend.setdefault(c, 0.0)
    full = apportion(spend, {c: sources.get(c, "") for c in want}, all_time_revenue(db) if srcs else {})
    return {c: full[c] for c in ids}


def unmatched(pb: dict, mapped_sources, top: int = 6) -> tuple[float, list]:
    """(revenue in range on sources no campaign carries, the biggest of them). Pure."""
    mapped = set(s for s in mapped_sources if s)
    items = sorted(((s, float((v or {}).get("revenue") or 0)) for s, v in pb.items() if s not in mapped),
                   key=lambda x: -x[1])
    items = [(s or "(no source)", round(r, 2)) for s, r in items if r > 0]
    return round(sum(r for _, r in items), 2), items[:top]


def outside_spend(db, models, records: list, range_key: str, start_day: str, end_day: str, names: dict, top: int = 6) -> tuple[float, list]:
    """(spend in the range on these campaigns — the ones the board doesn't show —, the biggest)."""
    if not records:
        return 0.0, []
    if range_key == "today":
        spend = {r.campaign_id: float(r.spend_today or 0) for r in records}
    else:
        from sqlalchemy import func
        ids = [r.campaign_id for r in records]
        spend = {}
        for n in range(0, len(ids), 500):
            for cid, s in (db.query(models.SpendSnapshot.campaign_id, func.sum(models.SpendSnapshot.spend))
                           .filter(models.SpendSnapshot.campaign_id.in_(ids[n:n + 500]),
                                   models.SpendSnapshot.day >= start_day, models.SpendSnapshot.day <= end_day)
                           .group_by(models.SpendSnapshot.campaign_id)):
                spend[cid] = float(s or 0)
    by = {r.campaign_id: r for r in records}
    items = sorted(((cid, s) for cid, s in spend.items() if s > 0), key=lambda x: -x[1])
    out = [{"name": (by[cid].campaign_name or cid)[:60], "account": names.get(by[cid].advertiser_id, by[cid].advertiser_id),
            "spend": round(s, 2)} for cid, s in items[:top]]
    return round(sum(s for _, s in items), 2), out


def issue_dates(db, models) -> dict:
    """When the issue scan FIRST saw each problem (Issue.detected_at survives rescans): per
    campaign ("c", id) and per account ("a", id) — the earliest. One query."""
    out: dict = {}
    for cat, adv, ref, at in (db.query(models.Issue.category, models.Issue.advertiser_id, models.Issue.ref, models.Issue.detected_at)
                              .filter(models.Issue.category.in_(("campaign", "account", "payment")))):
        if at is None:
            continue
        key = ("c", ref) if cat == "campaign" and ref else ("a", adv)
        if key[1] and (key not in out or at < out[key]):
            out[key] = at
    return out


def error_at(health: dict | None, acct, blocked_by_account: bool, campaign_id: str = "", issues: dict | None = None):
    """When a blocked campaign's problem started (naive UTC): the issue scan's first sighting of
    it (campaign, then account), the account's status change for an account-level block, or its
    ad groups' state change. None = unknown (counts as recent). Pure."""
    d = None
    issues = issues or {}
    if campaign_id:
        d = issues.get(("c", campaign_id))
    if d is None and acct is not None:
        d = issues.get(("a", getattr(acct, "advertiser_id", "")))
    if d is None and blocked_by_account and acct is not None:
        d = getattr(acct, "status_changed_at", None)
    if d is None and health:
        d = health.get("since")
    if d is not None and getattr(d, "tzinfo", None):
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d
