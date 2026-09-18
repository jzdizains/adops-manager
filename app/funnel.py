"""Lander funnel — what happens on the pages between the ad click and the offer.

The pages are static (Hostinger), so they cannot keep data. Each page sends ONE
`navigator.sendBeacon` per step to POST /t/lp (text/plain body, so no CORS preflight,
never blocks the page, survives navigation):

    /start   view      the prelander was painted
    /start   engaged   a person: the page was VISIBLE for a few seconds, or was touched/scrolled
    /start   continue  Continue was pressed
    /play    view      the lander was painted (via=continue when it came through /start's button,
                       carrying /start's visitor id so both pages count ONE person)
    /play    engaged   (same rule)
    /play    cta       an offer CTA was pressed

"view" counts everything that painted the page — preloaders, link previews and bots
included — so the step rates are measured against "engaged", the humans.

Counted by DISTINCT visitor (the page's stable `tmp_vid`) per step and source, so a
reload is not a second view. Conversions come from the postbacks (same source).

Cheap by design (512 MB box, many users): one INSERT per beacon, aggregates by SQL
GROUP BY (never rows into Python), a global rate cap so a flood cannot bloat the
database, and pruning of rows older than KEEP_DAYS at most once an hour.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

PAGES = ("start", "play")
STEPS = ("view", "engaged", "continue", "cta")
# v124: pages built by the dashboard beacon under their slug, with a few more steps
LANDER_STEPS = ("view", "engaged", "continue", "escaped", "escape_miss", "gate", "route", "cta")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_slugs: dict = {"at": None, "set": set()}          # known lander slugs, re-read at most once a minute
STEP_ORDER = (("start", "view"), ("start", "engaged"), ("start", "continue"),
              ("play", "view"), ("play", "engaged"), ("play", "cta"))
KEEP_DAYS = 30
MAX_PER_MINUTE = 1200          # beacons accepted per minute, in total — far above real traffic, far below a flood

_bucket = {"minute": None, "n": 0}
_prune_at = [datetime.min]


def _utcnow() -> datetime:
    return datetime.utcnow()


def _clean(v, n: int) -> str:
    v = str(v or "").strip()
    return "" if ("{" in v or "}" in v or v.startswith("__")) else v[:n]      # unreplaced macros never get stored


def known_slugs(db: Session) -> set:
    """Slugs of the dashboard's landers (cached a minute: beacons are frequent, landers are not)."""
    now = _utcnow()
    if _slugs["at"] is None or now - _slugs["at"] > timedelta(minutes=1):
        try:
            _slugs["set"] = {r[0] for r in db.query(models.Lander.slug).filter(models.Lander.enabled == True)}  # noqa: E712
        except Exception:      # noqa: BLE001 — a beacon never fails on a lookup
            _slugs["set"] = set()
        _slugs["at"] = now
    return _slugs["set"]


def accept(db: Session, d: dict, ip: str = "", user_agent: str = "") -> tuple[bool, str]:
    """Validate + store one beacon. Returns (stored, reason). ip / user agent (the beacon
    request's) are kept on VIEW rows only — the Events API match signals for a conversion
    that comes back through ClickFlare, where no Click row exists."""
    page = str(d.get("page") or "")
    step = str(d.get("step") or "")
    legacy = page in PAGES and step in STEPS and (page, step) in STEP_ORDER
    built = bool(SLUG_RE.match(page)) and page not in PAGES and step in LANDER_STEPS and page in known_slugs(db)
    if not (legacy or built):
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
        campaign_id=_clean(d.get("cid"), 40),
        via="continue" if str(d.get("via") or "") == "continue" else "",
        bucket=_clean(d.get("bucket"), 40), inapp=_clean(d.get("inapp"), 16), os=_clean(d.get("os"), 8),
        ttclid=_clean(d.get("ttclid"), 2000) if isinstance(d.get("ttclid"), str) and len(d.get("ttclid")) > 1 else "",   # older pages send 1/0
        ref=_clean(d.get("ref"), 400),
        ip=(ip or "")[:64] if step == "view" else "", ua=(user_agent or "")[:400] if step == "view" else "",
        ttp=_clean(d.get("ttp"), 120) if step == "view" else "")
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


def rows(db: Session, start_naive: datetime, end_naive: datetime, conversions: dict[str, dict] | None = None,
         source: str | None = None) -> list[dict]:
    """One row per source: distinct visitors at each step + conversions/revenue
    (from the postbacks, keyed by source), plus the step-to-step rates.
    `source` narrows the query to one campaign (the campaign drawer)."""
    q = (db.query(models.LanderEvent.source, models.LanderEvent.page, models.LanderEvent.step,
                  func.count(func.distinct(models.LanderEvent.vid)), func.count(models.LanderEvent.id),
                  func.sum(models.LanderEvent.has_ttclid))
           .filter(models.LanderEvent.created_at >= start_naive, models.LanderEvent.created_at < end_naive))
    if source is not None:
        q = q.filter(models.LanderEvent.source == source)
    q = q.group_by(models.LanderEvent.source, models.LanderEvent.page, models.LanderEvent.step)
    # /play visitors who came through /start's Continue button (the hand-off), per source
    aq = (db.query(models.LanderEvent.source, func.count(func.distinct(models.LanderEvent.vid)))
            .filter(models.LanderEvent.created_at >= start_naive, models.LanderEvent.created_at < end_naive,
                    models.LanderEvent.page == "play", models.LanderEvent.step == "view",
                    models.LanderEvent.via == "continue"))
    if source is not None:
        aq = aq.filter(models.LanderEvent.source == source)
    arrived = {(src or ""): int(n or 0) for src, n in aq.group_by(models.LanderEvent.source)}
    by_src: dict[str, dict] = {}
    for src, page, step, visitors, hits, with_tt in q:
        r = by_src.setdefault(src or "", {"source": src or "", "start_view": 0, "start_engaged": 0, "start_continue": 0,
                                          "play_arrived": 0, "play_view": 0, "play_engaged": 0, "play_cta": 0, "hits": 0, "with_ttclid": 0})
        r["play_arrived"] = arrived.get(src or "", 0)
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
        # people, not paints: every rate is against the engaged count of its page
        r["r_engaged"] = _rate(r["start_engaged"], r["start_view"])
        r["r_continue"] = _rate(r["start_continue"], r["start_engaged"])
        r["r_arrive"] = _rate(r["play_arrived"], r["start_continue"])      # the hand-off: pressed → arrived
        r["r_play_engaged"] = _rate(r["play_engaged"], r["play_view"])
        r["r_cta"] = _rate(r["play_cta"], r["play_engaged"])
        r["r_conv"] = _rate(r["conversions"], r["play_cta"])
        r["r_ttclid"] = _rate(r["with_ttclid"], r["start_view"])
        out.append(r)
    out.sort(key=lambda r: (-r["start_view"], r["source"]))
    return out


def _rate(a: int, b: int):
    return (a / b) if b else None
