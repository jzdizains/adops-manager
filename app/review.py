"""TikTok's review verdict per launch, kept — and summed up on every post / creative / spark.

The sweep already reads every ad group's status (adgroup_stats → AdgroupState). When a
campaign this tool launched shows an ad group delivering, the launch is marked "approved";
when every live ad group is AUDIT_DENY it is marked "rejected". The verdict stays on the
launch log after the campaign is paused or deleted, so a post that was rejected three
times says so in the pickers before it is launched a fourth time.

Pure helpers are tested directly; the DB readers are small batched queries.
"""
from __future__ import annotations

from datetime import datetime

APPROVED, REJECTED, PENDING = "approved", "rejected", "pending"


def _up(s) -> str:
    return str(s or "").upper()


def campaign_verdict(groups: list[dict]) -> str:
    """approved | rejected | pending | '' from a campaign's ad-group statuses.
    Approved once any group delivers (or has spent out its budget / run its course — only an
    approved ad does that); rejected when every group still switched on is AUDIT_DENY."""
    secs = [_up(g.get("secondary_status")) for g in groups]
    if any(("DELIVERY_OK" in s or "BUDGET_EXCEED" in s or "TIME_DONE" in s) for s in secs):
        return APPROVED
    live = [s for g, s in zip(groups, secs) if _up(g.get("operation_status")) != "DISABLE"
            and "CAMPAIGN_DISABLE" not in s and "ADGROUP_STATUS_DISABLE" not in s]
    if live and all("AUDIT_DENY" in s for s in live):
        return REJECTED
    if any(s in ("ADGROUP_STATUS_AUDIT", "ADGROUP_STATUS_REAUDIT") or s.endswith("_STATUS_CREATE") for s in secs):
        return PENDING
    return ""


def record(db, models, by_campaign: dict[str, list[dict]]) -> int:
    """Write approved / rejected onto the launch logs of these campaigns (only on change)."""
    verdicts = {cid: campaign_verdict(gs) for cid, gs in by_campaign.items() if cid}
    verdicts = {c: v for c, v in verdicts.items() if v in (APPROVED, REJECTED)}
    if not verdicts:
        return 0
    n = 0
    now = datetime.utcnow()
    ids = list(verdicts)
    for i in range(0, len(ids), 500):
        for lg in (db.query(models.LaunchLog).filter(models.LaunchLog.campaign_id.in_(ids[i:i + 500]))):
            v = verdicts.get(lg.campaign_id)
            if v and (lg.review or "") != v:
                lg.review, lg.review_at = v, now
                n += 1
    return n


PENDING_WINDOW_H = 72       # a launch with no verdict yet is "in review" this long; after that "unknown"


def _verdict_of(lg, now) -> str:
    v = getattr(lg, "review", "") or ""
    if v:
        return v
    at = getattr(lg, "created_at", None)
    return PENDING if at and (now - at).total_seconds() < PENDING_WINDOW_H * 3600 else "unknown"


def summary(logs, now: datetime | None = None) -> dict:
    """{tests, approved, rejected, pending, last, last_at} over one post's / creative's launches.
    A launch counts when a campaign came of it (ok, or partly live)."""
    now = now or datetime.utcnow()
    rows = [lg for lg in logs if getattr(lg, "ok", False) or (getattr(lg, "campaign_id", "") or "")]
    rows.sort(key=lambda lg: getattr(lg, "created_at", None) or datetime.min)
    out = {"tests": len(rows), "approved": 0, "rejected": 0, "pending": 0, "unknown": 0, "last": "", "last_at": ""}
    for lg in rows:
        v = _verdict_of(lg, now)
        out[v] = out.get(v, 0) + 1
    if rows:
        last = rows[-1]
        out["last"] = _verdict_of(last, now)
        at = getattr(last, "created_at", None)
        out["last_at"] = at.strftime("%Y-%m-%d") if at else ""
    return out


def label(s: dict) -> str:
    if not s or not s.get("tests"):
        return ""
    parts = []
    if s.get("approved"):
        parts.append(f"✓ {s['approved']} approved")
    if s.get("rejected"):
        parts.append(f"✕ {s['rejected']} rejected")
    if s.get("pending"):
        parts.append(f"{s['pending']} in review")
    return " · ".join(parts)


def _group(logs, key) -> dict:
    by: dict = {}
    for lg in logs:
        k = key(lg)
        if k is not None:
            by.setdefault(k, []).append(lg)
    return {k: summary(v) for k, v in by.items()}


def for_sparks(db, models, spark_ids) -> dict[int, dict]:
    ids = [int(x) for x in spark_ids if x]
    logs: list = []
    for i in range(0, len(ids), 500):
        logs += db.query(models.LaunchLog).filter(models.LaunchLog.spark_code_id.in_(ids[i:i + 500])).all()
    return _group(logs, lambda lg: lg.spark_code_id)


def for_creatives(db, models, creative_ids) -> dict[int, dict]:
    """Launch logs carry creative_id from v146; older launches are found through the
    creative's own used_campaign_id."""
    ids = [int(x) for x in creative_ids if x]
    if not ids:
        return {}
    logs: list = []
    for i in range(0, len(ids), 500):
        logs += db.query(models.LaunchLog).filter(models.LaunchLog.creative_id.in_(ids[i:i + 500])).all()
    have = {lg.creative_id for lg in logs}
    legacy = {}
    for i in range(0, len(ids), 500):
        for c in (db.query(models.Creative.id, models.Creative.used_campaign_id)
                  .filter(models.Creative.id.in_(ids[i:i + 500]), models.Creative.used_campaign_id != "")):
            if c[0] not in have and c[1]:
                legacy[c[1]] = c[0]
    if legacy:
        for lg in db.query(models.LaunchLog).filter(models.LaunchLog.campaign_id.in_(list(legacy)[:500])):
            lg_c = legacy.get(lg.campaign_id)
            if lg_c is not None:
                logs.append(_Shim(lg, lg_c))
    return _group(logs, lambda lg: lg.creative_id)


class _Shim:
    """A legacy launch log seen as belonging to a creative (never written back)."""
    def __init__(self, lg, creative_id):
        self.ok, self.campaign_id, self.review = lg.ok, lg.campaign_id, lg.review
        self.created_at, self.creative_id = lg.created_at, creative_id


def for_items(db, models, sc, item_ids) -> dict[str, dict]:
    """Per TikTok post id (profile videos): through the workspace's spark rows for that post."""
    ids = [str(x) for x in item_ids if x]
    if not ids:
        return {}
    spark_of: dict[int, str] = {}
    for i in range(0, len(ids), 500):
        q = db.query(models.SparkCode.id, models.SparkCode.tiktok_item_id).filter(
            models.SparkCode.tiktok_item_id.in_(ids[i:i + 500]))
        if sc is not None:
            q = sc.owned(q, models.SparkCode)            # this workspace's rows only
        for sid, item in q:
            spark_of[sid] = item
    if not spark_of:
        return {}
    logs: list = []
    sids = list(spark_of)
    for i in range(0, len(sids), 500):
        logs += db.query(models.LaunchLog).filter(models.LaunchLog.spark_code_id.in_(sids[i:i + 500])).all()
    return _group(logs, lambda lg: spark_of.get(lg.spark_code_id))


def item_date(item_id) -> str:
    """A TikTok post id carries its creation time in the top 32 bits (unix seconds)."""
    try:
        ts = int(str(item_id)) >> 32
    except (TypeError, ValueError):
        return ""
    if not 1_400_000_000 < ts < 4_000_000_000:          # 2014 … 2096: anything else isn't a post id
        return ""
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
