"""Past date ranges from the database — not one live TikTok report per account per page load.

Every sweep already writes per-campaign day rows (SpendSnapshot: spend, conversions) and
per-ad-group day rows (AdgroupSnapshot: spend, impressions, clicks, conversions). Summing
them gives yesterday / 7 d / 30 d / month-to-date / a custom range at database speed. Only an
account with NO saved day in the range (a new install, an account the sweep never saw) is
still asked live, and "?live=1" forces the old behaviour.
"""
from __future__ import annotations


def combine(spend_rows, ag_rows) -> dict[str, dict]:
    """{campaign_id: metrics} (the Campaigns page's shape) from
    spend_rows = [(campaign_id, spend, conversions)] and
    ag_rows = [(campaign_id, spend, impressions, clicks, conversions)]. Pure.
    Spend and conversions come from the campaign rows (every campaign, live or not);
    impressions and clicks from the ad-group rows (kept while a campaign runs)."""
    out: dict[str, dict] = {}
    for cid, sp, conv in spend_rows:
        m = out.setdefault(str(cid), {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0})
        m["spend"] += float(sp or 0)
        m["conversions"] += int(conv or 0)
    ag: dict[str, list] = {}
    for cid, sp, imp, clk, conv in ag_rows:
        a = ag.setdefault(str(cid), [0.0, 0, 0, 0])
        a[0] += float(sp or 0); a[1] += int(imp or 0); a[2] += int(clk or 0); a[3] += int(conv or 0)
    for cid, (sp, imp, clk, conv) in ag.items():
        m = out.setdefault(cid, {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0})
        m["impressions"], m["clicks"] = imp, clk
        if not m["spend"]:                       # no campaign-level day rows: the ad groups' own
            m["spend"], m["conversions"] = sp, conv
    for m in out.values():
        sp, imp, clk, conv = m["spend"], m["impressions"], m["clicks"], m["conversions"]
        m["ctr"] = round(clk / imp * 100, 2) if imp else 0.0
        m["cpc"] = round(sp / clk, 4) if clk else 0.0
        m["cpm"] = round(sp / imp * 1000, 4) if imp else 0.0
        m["cpa"] = round(sp / conv, 4) if conv else 0.0
    return out


def metrics(db, models, advertiser_ids, s_day: str, e_day: str) -> tuple[dict[str, dict], set[str]]:
    """({campaign_id: metrics}, advertiser ids that have at least one saved day in the range)."""
    from sqlalchemy import func
    ids = [a for a in advertiser_ids if a]
    spend_rows, ag_rows, covered = [], [], set()
    S, A = models.SpendSnapshot, models.AdgroupSnapshot
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for aid, cid, sp, conv in (db.query(S.advertiser_id, S.campaign_id, func.sum(S.spend), func.sum(S.conversions))
                                   .filter(S.advertiser_id.in_(chunk), S.day >= s_day, S.day <= e_day)
                                   .group_by(S.advertiser_id, S.campaign_id)):
            covered.add(aid)
            spend_rows.append((cid, sp, conv))
        for aid, cid, sp, imp, clk, conv in (db.query(A.advertiser_id, A.campaign_id, func.sum(A.spend), func.sum(A.impressions),
                                                      func.sum(A.clicks), func.sum(A.conversions))
                                             .filter(A.advertiser_id.in_(chunk), A.day >= s_day, A.day <= e_day)
                                             .group_by(A.advertiser_id, A.campaign_id)):
            covered.add(aid)
            ag_rows.append((cid, sp, imp, clk, conv))
    return combine(spend_rows, ag_rows), covered
