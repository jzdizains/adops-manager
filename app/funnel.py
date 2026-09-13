"""Lander funnel — what happens on the pages between the ad click and the offer.

The pages are static (Hostinger), so they cannot keep data. Each page sends ONE
`navigator.sendBeacon` per step to POST /t/lp (text/plain body, so no CORS preflight,
never blocks the page, survives navigation):

    /start   view      the prelander was painted
    /start   continue  Continue was pressed
    /play    view      the lander was painted
    /play    cta       an offer CTA was pressed

Counted by DISTINCT visitor (the page's stable `tmp_vid`) per step and source, so a
reload is not a second view. Conversions come from the postbacks (same source).

Cheap by design (512 MB box, many users): one INSERT per beacon, aggregates by SQL
GROUP BY (never rows into Python), a global rate cap so a flood cannot bloat the
database, and pruning of rows older than KEEP_DAYS at most once an hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

PAGES = ("start", "play")
STEPS = ("view", "continue", "cta")
STEP_ORDER = (("start", "view"), ("start", "continue"), ("play", "view"), ("play", "cta"))
KEEP_DAYS = 30
MAX_PER_MINUTE = 1200          # beacons accepted per minute, in total — far above real traffic, far below a flood

_bucket = {"minute": None, "n": 0}
_prune_at = [datetime.min]


def _utcnow() -> datetime:
    return datetime.utcnow()


def _clean(v, n: int) -> str:
    v = str(v or "").strip()
    return "" if ("{" in v or "}" in v or v.startswith("__")) else v[:n]      # unreplaced macros never get stored


def accept(db: Session, d: dict) -> tuple[bool, str]:
    """Validate + store one beacon. Returns (stored, reason)."""
    page = str(d.get("page") or "")
    step = str(d.get("step") or "")
    if page not in PAGES or step not in STEPS or (page, step) not in STEP_ORDER:
        return False, "unknown page/step"
    now = _utcnow()
    minute = now.replace(second=0, microsecond=0)
    if _bucket["minute"] != minute:
        _bucket["minute"], _bucket["n"] = minute, 0
    if _bucket["n"] >= MAX_PER_MINUTE:
        return False, "rate"
    _bucket["n"] += 1
    row = models.LanderEvent(
        source=_clean(d.get("source"), 200), page=page, step=step,
        vid=_clean(d.get("vid"), 64), has_ttclid=bool(d.get("ttclid")),
        campaign_id=_clean(d.get("cid"), 40))
    db.add(row)
    _prune(db)
    return True, ""


def _prune(db: Session) -> None:
    now = _utcnow()
    if now - _prune_at[0] < timedelta(hours=1):
        return
    _prune_at[0] = now
    try:
        (db.query(models.LanderEvent)
           .filter(models.LanderEvent.created_at < now - timedelta(days=KEEP_DAYS))
           .delete(synchronize_session=False))
    except Exception:      # noqa: BLE001 — pruning must never fail a beacon
        pass


def rows(db: Session, start_naive: datetime, end_naive: datetime, conversions: dict[str, dict] | None = None) -> list[dict]:
    """One row per source: distinct visitors at each step + conversions/revenue
    (from the postbacks, keyed by source), plus the step-to-step rates."""
    q = (db.query(models.LanderEvent.source, models.LanderEvent.page, models.LanderEvent.step,
                  func.count(func.distinct(models.LanderEvent.vid)), func.count(models.LanderEvent.id),
                  func.sum(models.LanderEvent.has_ttclid))
           .filter(models.LanderEvent.created_at >= start_naive, models.LanderEvent.created_at < end_naive)
           .group_by(models.LanderEvent.source, models.LanderEvent.page, models.LanderEvent.step))
    by_src: dict[str, dict] = {}
    for src, page, step, visitors, hits, with_tt in q:
        r = by_src.setdefault(src or "", {"source": src or "", "start_view": 0, "start_continue": 0, "play_view": 0, "play_cta": 0,
                                          "hits": 0, "with_ttclid": 0})
        r[f"{page}_{step}"] = int(visitors or 0)
        r["hits"] += int(hits or 0)
        if page == "start" and step == "view":
            r["with_ttclid"] = int(with_tt or 0)
    for src, c in (conversions or {}).items():
        if src in by_src:
            by_src[src]["conversions"] = int(c.get("conversions") or 0)
            by_src[src]["revenue"] = float(c.get("revenue") or 0)
    out = []
    for r in by_src.values():
        r.setdefault("conversions", 0)
        r.setdefault("revenue", 0.0)
        r["r_continue"] = _rate(r["start_continue"], r["start_view"])
        r["r_arrive"] = _rate(r["play_view"], r["start_continue"])
        r["r_cta"] = _rate(r["play_cta"], r["play_view"])
        r["r_conv"] = _rate(r["conversions"], r["play_cta"])
        r["r_ttclid"] = _rate(r["with_ttclid"], r["start_view"])
        out.append(r)
    out.sort(key=lambda r: (-r["start_view"], r["source"]))
    return out


def _rate(a: int, b: int):
    return (a / b) if b else None
