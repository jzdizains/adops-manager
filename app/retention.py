"""Retention (v155.30) — the tables that grow with activity and never had a ceiling.

Everything the dashboard shows day to day comes from the recent rows; what these tables hold
past the horizon is history nobody opens and every page still had to read past (the source
map, P&L and the Campaigns board walk the whole launch log). Each table below keeps a long,
plain window and is trimmed on the slow sweep in small chunks, inside a time budget, so a
first run on a big database never holds the writer lock for long.

    run(db, models, budget_s)   → {"table": rows deleted, …}   (never raises)
    plan()                      → the table, for the Capacity card

Deliberately NOT here: ad accounts, campaigns' spend history inside the window, presets,
creatives, users, settings. Nothing that a page still needs is touched.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger("adops.retention")

CHUNK = 5000              # rows per DELETE — short transactions, the site keeps answering meanwhile
BUDGET_S = 20.0           # per slow sweep; what is left waits for the next one

# (model name, timestamp column, days kept, extra filter name or None, what it is)
RULES: tuple[tuple[str, str, int, str | None, str], ...] = (
    ("Alert",           "created_at", 90,  None,     "Inbox notices"),
    ("Click",           "created_at", 90,  None,     "ad clicks (tracking)"),
    ("PostbackEvent",   "created_at", 120, None,     "postbacks received"),
    ("RuleAction",      "created_at", 180, None,     "rule actions"),
    ("TopUp",           "created_at", 365, None,     "top-up records"),
    ("ActivityEvent",   "at",         180, None,     "activity feed"),
    ("LaunchQueueItem", "created_at", 30,  "finished", "launch queue (done / failed)"),
    ("ConversionSample", "sampled_at", 90, None,     "conversion samples (legacy)"),
    ("EscapeTest",      "created_at", 90,  None,     "escape tests"),
    ("LaunchLog",       "created_at", 400, "campaign_gone", "launch log (per account; a campaign still on TikTok keeps its row)"),
    ("SpendSnapshot",   "updated_at", 400, None,     "spend history (per campaign per day)"),
)


def plan() -> list[dict]:
    return [{"model": m, "days": d, "what": w, "only": only or ""} for m, _c, d, only, w in RULES]


def _cutoff(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.utcnow()) - timedelta(days=days)


def _extra(db, models, model, only: str | None):
    if only == "finished":
        return model.status.in_(("done", "failed"))
    if only == "campaign_gone":          # the source map of a live campaign must survive any age
        return model.campaign_id.notin_(db.query(models.CampaignRecord.campaign_id))
    return None


def run(db, models, budget_s: float = BUDGET_S, now: datetime | None = None) -> dict:
    """Trim every table in RULES, oldest first, CHUNK rows at a time, until done or out of time."""
    t0 = time.monotonic()
    out: dict[str, int] = {}
    for name, col, days, only, _what in RULES:
        model = getattr(models, name, None)
        if model is None:
            continue
        column = getattr(model, col, None)
        if column is None:
            continue
        deleted = 0
        try:
            while time.monotonic() - t0 < budget_s:
                q = db.query(model.id).filter(column < _cutoff(days, now))
                extra = _extra(db, models, model, only)
                if extra is not None:
                    q = q.filter(extra)
                ids = [r[0] for r in q.order_by(model.id).limit(CHUNK)]
                if not ids:
                    break
                n = db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
                db.commit()
                deleted += int(n or 0)
                if len(ids) < CHUNK:
                    break
        except Exception:  # noqa: BLE001 — one table's trouble never stops the others
            db.rollback()
            log.exception("retention: %s", name)
        if deleted:
            out[name] = deleted
    # campaign rows of accounts whose access is gone for good (a month): the sync re-creates
    # them the moment access returns, and the Campaigns board stops reading them meanwhile
    try:
        gone = [r[0] for r in db.query(models.AdAccount.advertiser_id)
                .filter(models.AdAccount.status == "ACCESS_LOST", models.AdAccount.enabled == False,   # noqa: E712
                        models.AdAccount.last_synced_at < _cutoff(30, now))]
        if gone and time.monotonic() - t0 < budget_s:
            n = 0
            for i in range(0, len(gone), 200):
                n += db.query(models.CampaignRecord).filter(models.CampaignRecord.advertiser_id.in_(gone[i:i + 200])).delete(synchronize_session=False)
                db.commit()
            if n:
                out["CampaignRecord"] = int(n)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("retention: CampaignRecord")
    if out:
        log.info("retention trimmed: %s", out)
    return out
