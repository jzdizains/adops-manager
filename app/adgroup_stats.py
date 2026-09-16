"""Per-ad-group numbers — the "Ad groups: active only" mode and the drawer's list.

The campaign sweep already pulls TikTok's campaign-level report per account. For the
accounts with live campaigns it now also pulls the AD-GROUP level (one more report
call) and the ad group list (status, name, created time), and upserts one
AdgroupSnapshot per ad group per local day.

A campaign switched to "active ad groups only" (drawer toggle, CampaignPref) has its
metrics recomputed from the ad groups whose status
was ENABLE at the last sweep — for the whole range, so an ad group paused an hour
ago is out for the whole day. Revenue follows the same rule through the ad group
id ClickFlare puts on the postback (tracking field 6); conversions that arrived
without one can't be split and are reported as "unsplit", never dropped silently.

History exists from the day this shipped; older days have no ad-group rows, so
"active only" for those days shows zeros with the row saying so.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, tiktok_api

log = logging.getLogger("adops.adgroup_stats")

KEEP_DAYS = 45
_prune_at = [datetime.min]


def flagged(db: Session) -> set[str]:
    """Campaign ids whose numbers should come from active ad groups only."""
    return {cid for (cid,) in db.query(models.CampaignPref.campaign_id)
            .filter(models.CampaignPref.ag_active_only == True)}          # noqa: E712


def set_flag(db: Session, campaign_id: str, on: bool) -> None:
    pref = db.query(models.CampaignPref).filter_by(campaign_id=str(campaign_id)).first()
    if pref is None:
        pref = models.CampaignPref(campaign_id=str(campaign_id))
        db.add(pref)
    pref.ag_active_only = bool(on)


def _f(m: dict, key: str) -> float:
    try:
        return float(m.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# sync (called from live_spend.sync_campaigns, per account, today)
# ---------------------------------------------------------------------------

def fetch_account(acct: models.AdAccount, campaign_ids: list[str], day: str,
                  report_metrics: list[str]) -> tuple[list[dict], dict[str, dict]] | None:
    """NETWORK ONLY — no database touched. Two calls: /adgroup/get/ (status, names) and
    the ad-group-level report. Returns (groups, metrics_by_adgroup), or None when the
    list call failed (nothing to write; yesterday's rows stay). Must run BEFORE the
    campaign sync opens its write transaction: SQLite has one writer, and holding the
    lock through seconds of TikTok calls is what made other jobs fail with
    "database is locked"."""
    if not campaign_ids or not acct.access_token:
        return None
    groups: list[dict] = []
    try:
        for i in range(0, len(campaign_ids), 100):          # the filter takes up to 100 ids
            page = 1
            while True:
                data = tiktok_api.list_adgroups(acct.access_token, acct.advertiser_id,
                                                campaign_ids[i:i + 100], page=page, page_size=100) or {}
                groups.extend(data.get("list") or [])
                info = data.get("page_info") or {}
                if page >= int(info.get("total_page") or 1) or page >= 20:
                    break
                page += 1
    except tiktok_api.TikTokError as e:
        log.info("adgroup list unavailable for %s: %s", acct.advertiser_id, e)
        return None
    metrics: dict[str, dict] = {}
    try:
        for r in tiktok_api.get_report(acct.access_token, acct.advertiser_id,
                                       dimensions=["adgroup_id"], metrics=report_metrics,
                                       start_date=day, end_date=day, data_level="AUCTION_ADGROUP",
                                       page_size=1000):
            metrics[str((r.get("dimensions") or {}).get("adgroup_id") or "")] = r.get("metrics") or {}
    except tiktok_api.TikTokError as e:
        log.info("adgroup report unavailable for %s: %s", acct.advertiser_id, e)
    return groups, metrics


def write_account(db: Session, acct: models.AdAccount, day: str, fetched) -> int:
    """DATABASE ONLY — upsert today's rows from what fetch_account returned. Fast: one
    read of today's rows for the account, then in-memory updates. Returns rows touched."""
    if not fetched:
        return 0
    groups, metrics = fetched
    existing = {row.adgroup_id: row for row in
                db.query(models.AdgroupSnapshot)
                  .filter(models.AdgroupSnapshot.advertiser_id == acct.advertiser_id,
                          models.AdgroupSnapshot.day == day)}
    n = 0
    for g in groups:
        agid = str(g.get("adgroup_id") or "")
        if not agid:
            continue
        row = existing.get(agid)
        if row is None:
            row = models.AdgroupSnapshot(advertiser_id=acct.advertiser_id, adgroup_id=agid, day=day,
                                         campaign_id=str(g.get("campaign_id") or ""))
            db.add(row)
            existing[agid] = row
        row.campaign_id = str(g.get("campaign_id") or row.campaign_id or "")
        row.adgroup_name = (g.get("adgroup_name") or "")[:200]
        row.operation_status = str(g.get("operation_status") or "")
        row.created_time = str(g.get("create_time") or "")[:19]
        m = metrics.get(agid, {})
        row.spend = _f(m, "spend")
        row.impressions = int(_f(m, "impressions"))
        row.clicks = int(_f(m, "clicks"))
        row.conversions = int(_f(m, "conversion"))
        n += 1
    try:
        from . import bid_bump
        bid_bump.observe(db, acct.advertiser_id, groups, metrics, day)     # idle-bid-bump watch: same data, no extra calls
    except Exception:  # noqa: BLE001 — the watch must never break the spend sync
        log.exception("bid watch update failed for %s", acct.advertiser_id)
    _prune(db)
    return n


def sync_account(db: Session, acct: models.AdAccount, campaign_ids: list[str], day: str,
                 report_metrics: list[str]) -> int:
    """fetch + write in one go — only for callers that hold no write transaction yet."""
    return write_account(db, acct, day, fetch_account(acct, campaign_ids, day, report_metrics))


def _prune(db: Session) -> None:
    now = datetime.utcnow()
    if now - _prune_at[0] < timedelta(hours=6):
        return
    _prune_at[0] = now
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        db.query(models.AdgroupSnapshot).filter(models.AdgroupSnapshot.day < cutoff).delete(synchronize_session=False)
    except Exception:      # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def status_now(db: Session, campaign_ids: list[str]) -> dict[str, dict[str, str]]:
    """campaign_id -> {adgroup_id: operation_status as of the latest day we have}."""
    ids = list({c for c in campaign_ids if c})
    if not ids:
        return {}
    latest: dict[str, tuple[str, str, str]] = {}      # adgroup -> (day, status, campaign)
    for cid, agid, day, st in (db.query(models.AdgroupSnapshot.campaign_id, models.AdgroupSnapshot.adgroup_id,
                                        models.AdgroupSnapshot.day, models.AdgroupSnapshot.operation_status)
                                 .filter(models.AdgroupSnapshot.campaign_id.in_(ids))):
        cur = latest.get(agid)
        if cur is None or day > cur[0]:
            latest[agid] = (day, st or "", cid)
    out: dict[str, dict[str, str]] = {}
    for agid, (_day, st, cid) in latest.items():
        out.setdefault(cid, {})[agid] = st
    return out


def active_metrics(db: Session, campaign_ids: list[str], start_day: str, end_day: str) -> dict[str, dict]:
    """campaign_id -> metrics summed over its ACTIVE ad groups in [start_day, end_day],
    plus what was hidden: {"spend", "impressions", "clicks", "conversions", "ctr", "cpc",
    "cpm", "cpa", "active_n", "total_n", "hidden_spend", "hidden_conversions", "has_rows"}."""
    ids = list({c for c in campaign_ids if c})
    if not ids:
        return {}
    status = status_now(db, ids)
    q = (db.query(models.AdgroupSnapshot.campaign_id, models.AdgroupSnapshot.adgroup_id,
                  func.sum(models.AdgroupSnapshot.spend), func.sum(models.AdgroupSnapshot.impressions),
                  func.sum(models.AdgroupSnapshot.clicks), func.sum(models.AdgroupSnapshot.conversions))
           .filter(models.AdgroupSnapshot.campaign_id.in_(ids),
                   models.AdgroupSnapshot.day >= start_day, models.AdgroupSnapshot.day <= end_day)
           .group_by(models.AdgroupSnapshot.campaign_id, models.AdgroupSnapshot.adgroup_id))
    out: dict[str, dict] = {}
    for cid in ids:
        st = status.get(cid, {})
        out[cid] = {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0,
                    "active_n": sum(1 for s in st.values() if s == "ENABLE"), "total_n": len(st),
                    "hidden_spend": 0.0, "hidden_conversions": 0, "has_rows": False}
    for cid, agid, sp, im, ck, cv in q:
        o = out.setdefault(cid, {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0, "active_n": 0,
                                 "total_n": 0, "hidden_spend": 0.0, "hidden_conversions": 0, "has_rows": False})
        o["has_rows"] = True
        if status.get(cid, {}).get(agid) == "ENABLE":
            o["spend"] += float(sp or 0); o["impressions"] += int(im or 0)
            o["clicks"] += int(ck or 0); o["conversions"] += int(cv or 0)
        else:
            o["hidden_spend"] += float(sp or 0); o["hidden_conversions"] += int(cv or 0)
    for o in out.values():
        o["ctr"] = (o["clicks"] / o["impressions"] * 100) if o["impressions"] else 0.0
        o["cpc"] = (o["spend"] / o["clicks"]) if o["clicks"] else 0.0
        o["cpm"] = (o["spend"] / o["impressions"] * 1000) if o["impressions"] else 0.0
        o["cpa"] = (o["spend"] / o["conversions"]) if o["conversions"] else 0.0
    return out


def active_revenue(db: Session, campaign_ids: list[str], start_naive: datetime, end_naive: datetime,
                   sources_by_cid: dict[str, str]) -> dict[str, dict]:
    """campaign_id -> {"revenue", "conversions", "unsplit_revenue", "unsplit_conversions"}:
    postbacks whose ad group is ACTIVE in that campaign count; postbacks with no ad
    group id (before the ClickFlare paste) are reported as unsplit for the source."""
    ids = list({c for c in campaign_ids if c})
    if not ids:
        return {}
    status = status_now(db, ids)
    owner: dict[str, str] = {agid: cid for cid, m in status.items() for agid in m}
    active = {agid for cid, m in status.items() for agid, s in m.items() if s == "ENABLE"}
    srcs = list({s for s in sources_by_cid.values() if s})
    out = {cid: {"revenue": 0.0, "conversions": 0, "unsplit_revenue": 0.0, "unsplit_conversions": 0} for cid in ids}
    if not srcs:
        return out
    unsplit: dict[str, dict] = {}
    q = (db.query(models.PostbackEvent.source, models.PostbackEvent.adgroup_id,
                  func.sum(models.PostbackEvent.revenue), func.sum(models.PostbackEvent.conversions))
           .filter(models.PostbackEvent.source.in_(srcs),
                   models.PostbackEvent.created_at >= start_naive, models.PostbackEvent.created_at < end_naive)
           .group_by(models.PostbackEvent.source, models.PostbackEvent.adgroup_id))
    for src, agid, rv, cv in q:
        agid = agid or ""
        if not agid:
            u = unsplit.setdefault(src, {"revenue": 0.0, "conversions": 0})
            u["revenue"] += float(rv or 0); u["conversions"] += int(cv or 0)
            continue
        cid = owner.get(agid)
        if cid in out and agid in active:
            out[cid]["revenue"] += float(rv or 0); out[cid]["conversions"] += int(cv or 0)
    for cid, src in sources_by_cid.items():
        if cid in out and src in unsplit:
            out[cid]["unsplit_revenue"] = unsplit[src]["revenue"]
            out[cid]["unsplit_conversions"] = unsplit[src]["conversions"]
    return out


def drawer_rows(db: Session, campaign_id: str, start_day: str, end_day: str) -> dict[str, dict]:
    """adgroup_id -> {spend, impressions, clicks, conversions, revenue, pb_conversions,
    status, created} for the drawer's list, over the range."""
    out: dict[str, dict] = {}
    latest: dict[str, str] = {}
    for agid, day, st, created, name in (db.query(models.AdgroupSnapshot.adgroup_id, models.AdgroupSnapshot.day,
                                                  models.AdgroupSnapshot.operation_status, models.AdgroupSnapshot.created_time,
                                                  models.AdgroupSnapshot.adgroup_name)
                                           .filter(models.AdgroupSnapshot.campaign_id == str(campaign_id))):
        if agid not in latest or day > latest[agid]:
            latest[agid] = day
            out.setdefault(agid, {})["status"] = st or ""
            out[agid]["created"] = created or ""
            out[agid]["name"] = name or ""
    for agid, sp, im, ck, cv in (db.query(models.AdgroupSnapshot.adgroup_id, func.sum(models.AdgroupSnapshot.spend),
                                         func.sum(models.AdgroupSnapshot.impressions), func.sum(models.AdgroupSnapshot.clicks),
                                         func.sum(models.AdgroupSnapshot.conversions))
                                   .filter(models.AdgroupSnapshot.campaign_id == str(campaign_id),
                                           models.AdgroupSnapshot.day >= start_day, models.AdgroupSnapshot.day <= end_day)
                                   .group_by(models.AdgroupSnapshot.adgroup_id)):
        o = out.setdefault(agid, {"status": "", "created": "", "name": ""})
        o.update({"spend": float(sp or 0), "impressions": int(im or 0), "clicks": int(ck or 0), "conversions": int(cv or 0)})
    for o in out.values():
        o.setdefault("spend", 0.0); o.setdefault("impressions", 0); o.setdefault("clicks", 0); o.setdefault("conversions", 0)
        o["revenue"] = 0.0; o["pb_conversions"] = 0
    return out


def revenue_by_adgroup(db: Session, source: str, start_naive: datetime, end_naive: datetime) -> dict[str, dict]:
    """adgroup_id -> {"revenue", "conversions"} for one source in the range ("" = unsplit)."""
    if not source:
        return {}
    out: dict[str, dict] = {}
    for agid, rv, cv in (db.query(models.PostbackEvent.adgroup_id, func.sum(models.PostbackEvent.revenue),
                                  func.sum(models.PostbackEvent.conversions))
                           .filter(models.PostbackEvent.source == source,
                                   models.PostbackEvent.created_at >= start_naive, models.PostbackEvent.created_at < end_naive)
                           .group_by(models.PostbackEvent.adgroup_id)):
        out[agid or ""] = {"revenue": float(rv or 0), "conversions": int(cv or 0)}
    return out

