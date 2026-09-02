"""Per-creative performance — which creative is actually making money.

Every launched creative records where it went (used_campaign_id /
used_advertiser_id) and carries a P&L `source`. That gives a clean join:

  spend    SpendSnapshot for the creative's campaign, over the range (DB-only)
  revenue  PostbackEvent for the creative's source, over the range (DB-only)
  engage   the linked CampaignRecord's cached TikTok numbers (latest sync:
           impressions / clicks / conversions) — these aren't stored per day,
           so they're labelled "latest sync" rather than pretending to be
           range-aware.

Variants made from one upload share `source_md5`, so rows can also be rolled
up into a *family* to see which ORIGINAL wins across all its copies.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, pnl_data, timeutil


def _slides(c) -> list[int]:
    if c.kind != "carousel":
        return []
    import json as _json
    try:
        return [int(x) for x in _json.loads(c.carousel_images or "[]")]
    except (ValueError, TypeError):
        return []


def rows(db: Session, start_utc: datetime, end_utc: datetime,
         today: bool = False) -> list[dict]:
    creatives = (db.query(models.Creative)
                 .filter(models.Creative.used_campaign_id != "").all())
    if not creatives:
        return []
    cids = list({c.used_campaign_id for c in creatives})
    camps = {c.campaign_id: c for c in
             db.query(models.CampaignRecord)
             .filter(models.CampaignRecord.campaign_id.in_(cids)).all()}
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}

    # spend per campaign over the range (snapshots); today → the fresher cache
    spend_by_cid: dict[str, float] = {}
    if today:
        for cid, c in camps.items():
            spend_by_cid[cid] = float(c.spend_today or 0)
    else:
        s_day = timeutil.local_date_str(start_utc)
        from datetime import timedelta
        e_day = timeutil.local_date_str(end_utc - timedelta(seconds=1))
        for cid, total in (db.query(models.SpendSnapshot.campaign_id,
                                    func.sum(models.SpendSnapshot.spend))
                           .filter(models.SpendSnapshot.campaign_id.in_(cids),
                                   models.SpendSnapshot.day >= s_day,
                                   models.SpendSnapshot.day <= e_day)
                           .group_by(models.SpendSnapshot.campaign_id)):
            spend_by_cid[cid] = float(total or 0)

    pb = pnl_data.revenue_by_source(db, start_utc, end_utc)
    # the creative's P&L key is its CAMPAIGN's source (campaign-name mode) —
    # fall back to the creative's own source (static mode)
    camp_src = pnl_data.campaign_source_map(db)
    def _src(c):
        return camp_src.get(c.used_campaign_id) or (c.source or "")
    # a source can be shared by several creatives (rare) — split evenly
    src_share: dict[str, int] = {}
    for c in creatives:
        k = _src(c)
        if k:
            src_share[k] = src_share.get(k, 0) + 1

    out = []
    for c in creatives:
        camp = camps.get(c.used_campaign_id)
        acct = accounts.get(c.used_advertiser_id)
        spend = spend_by_cid.get(c.used_campaign_id, 0.0)
        key = _src(c)
        share = 1.0 / src_share.get(key, 1) if key else 0.0
        p = pb.get(key, {}) if key else {}
        revenue = float(p.get("revenue", 0.0)) * share
        pb_conv = float(p.get("conversions", 0)) * share
        pb_clicks = float(p.get("clicks", 0)) * share
        out.append({
            "c": c, "camp": camp, "acct": acct,
            "campaign_name": (camp.campaign_name if camp else "") or c.used_campaign_id,
            "account_name": (acct.advertiser_name if acct else "") or c.used_advertiser_id,
            "active": bool(camp and camp.operation_status == "ENABLE"),
            "spend": spend, "revenue": revenue, "profit": revenue - spend,
            "roas": (revenue / spend) if spend else 0.0,
            "pb_conversions": pb_conv, "pb_clicks": pb_clicks,
            "impressions": int(camp.impressions or 0) if camp else 0,
            "clicks": int(camp.clicks or 0) if camp else 0,
            "conversions": int(camp.conversions or 0) if camp else 0,
            "ctr": float(camp.ctr or 0) if camp else 0.0,
            "cpa": float(camp.cpa or 0) if camp else 0.0,
            "family": c.source_md5 or c.md5 or "",
            "slides": _slides(c),
        })
    return out


def families(perf_rows: list[dict]) -> list[dict]:
    """Roll variant rows up by original upload (source_md5)."""
    fam: dict[str, dict] = {}
    for r in perf_rows:
        key = r["family"] or f"solo:{r['c'].id}"
        f = fam.setdefault(key, {"key": key, "name": "", "n": 0, "spend": 0.0,
                                 "revenue": 0.0, "profit": 0.0, "rows": [],
                                 "best": None})
        f["n"] += 1
        f["spend"] += r["spend"]; f["revenue"] += r["revenue"]; f["profit"] += r["profit"]
        f["rows"].append(r)
        if f["best"] is None or r["roas"] > f["best"]["roas"]:
            f["best"] = r
        if not f["name"]:
            # family name = the upload's base name, minus the _vN suffix
            import re as _re
            f["name"] = _re.sub(r"_v\d+(?=\.\w+$)", "", r["c"].file_name or r["c"].name or key)
    for f in fam.values():
        f["roas"] = (f["revenue"] / f["spend"]) if f["spend"] else 0.0
    # two different originals with the same base name must stay tellable apart
    seen: dict[str, int] = {}
    for f in fam.values():
        seen[f["name"]] = seen.get(f["name"], 0) + 1
    for f in fam.values():
        if seen[f["name"]] > 1:
            f["name"] = f"{f['name']} · {f['key'][:6]}"
    return sorted(fam.values(), key=lambda f: f["profit"], reverse=True)


SORTS = {
    "roas": lambda r: r["roas"], "revenue": lambda r: r["revenue"],
    "profit": lambda r: r["profit"], "spend": lambda r: r["spend"],
    "conversions": lambda r: r["conversions"], "ctr": lambda r: r["ctr"],
}
