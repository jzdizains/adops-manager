"""Parity watch — notice when TikTok is using an option this launcher doesn't offer.

Every sweep reads the live campaigns and ad groups back from TikTok. Each value of
the fields below is compared with what the launcher can set; anything unknown is
recorded ONCE as an Inbox notice ("TikTok has an option we don't offer yet") and in
Diagnostics with the campaign it was seen on. That is how a new optimisation goal,
bid type or placement surfaces the first day it is used in Ads Manager, instead of
when someone trips over it.

Cheap: pure dict lookups over data the sweep already holds; one query per NEW value.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import models

log = logging.getLogger("adops.parity")

# what the launcher can produce today (kept next to the code that produces it)
def _known() -> dict[str, set[str]]:
    from .routes import launch
    goals = {k for k, _ in launch.OPT_GOAL_OPTIONS if k} | set(launch.GOAL_LABELS) - {""}
    goals |= {g for rule in launch.OBJECTIVE_RULES.values() for g in rule["goals"]}
    goals |= {g for g, _ in launch.ENGAGED_CANDIDATES}          # Traffic · Engaged session (probed)
    events = {k for k, _ in launch.PIXEL_EVENTS} | {"", "LANDING_PAGE_VIEW"} | {e for _, e in launch.ENGAGED_CANDIDATES}
    return {
        "objective_type": set(launch.OBJECTIVES),
        "optimization_goal": goals,
        "optimization_event": events,
        "billing_event": {"CPC", "CPM", "CPV", "OCPM"},
        "bid_type": {"BID_TYPE_NO_BID", "BID_TYPE_CUSTOM"},
        "placement": {"PLACEMENT_TIKTOK"},
        "placement_type": {"PLACEMENT_TYPE_NORMAL", "PLACEMENT_TYPE_AUTOMATIC"},
        "pacing": {k for k, _ in launch.PACING_OPTIONS},
        "budget_mode": {"BUDGET_MODE_DAY", "BUDGET_MODE_TOTAL", "BUDGET_MODE_INFINITE", "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"},
        "promotion_type": {"WEBSITE", "LEAD_GENERATION", ""},
        "deep_bid_type": {"", "DEFAULT"},
    }


LABEL = {
    "objective_type": "campaign objective", "optimization_goal": "optimisation goal",
    "optimization_event": "optimisation event", "billing_event": "billing event", "bid_type": "bid strategy",
    "placement": "placement", "placement_type": "placement mode", "pacing": "delivery type",
    "budget_mode": "budget mode", "promotion_type": "promotion type", "deep_bid_type": "value bidding",
}

_seen_this_process: set[tuple[str, str]] = set()      # (field, value) already reported since start


def _fields_of_adgroup(g: dict) -> list[tuple[str, str]]:
    out = []
    for f in ("optimization_goal", "optimization_event", "billing_event", "bid_type", "placement_type",
              "pacing", "budget_mode", "promotion_type", "deep_bid_type"):
        v = g.get(f)
        if v is not None:
            out.append((f, str(v)))
    for p in (g.get("placements") or []):
        out.append(("placement", str(p)))
    return out


def observe(db: Session, acct, campaigns: list[dict], adgroups: list[dict] | None) -> int:
    """Compare what TikTok returned for this account with what the launcher offers.
    Returns how many NEW (field, value) pairs were reported. Never raises."""
    try:
        known = _known()
        names = {str(c.get("campaign_id") or ""): (c.get("campaign_name") or "") for c in (campaigns or [])}
        sightings: dict[tuple[str, str], str] = {}
        for c in (campaigns or []):
            for f in ("objective_type", "budget_mode"):
                v = c.get(f)
                if v is not None and str(v) not in known.get(f, set()):
                    sightings.setdefault((f, str(v)), c.get("campaign_name") or str(c.get("campaign_id") or ""))
        for g in (adgroups or []):
            camp = names.get(str(g.get("campaign_id") or ""), str(g.get("campaign_id") or ""))
            for f, v in _fields_of_adgroup(g):
                if v not in known.get(f, set()):
                    sightings.setdefault((f, v), camp)
        new = 0
        for (f, v), camp in sightings.items():
            if (f, v) in _seen_this_process:
                continue
            _seen_this_process.add((f, v))
            ref = f"{f}:{v}"[:200]
            if db.query(models.Alert.id).filter_by(kind="parity", ref_id=ref).first():
                continue                       # reported before (acknowledged or not) — once is enough
            msg = (f"TikTok is using a {LABEL.get(f, f)} value this launcher doesn't offer yet: {v} "
                   f"(seen on “{camp[:60]}”, {getattr(acct, 'advertiser_name', '') or getattr(acct, 'advertiser_id', '')}). "
                   f"Ads Manager has it; the preset form does not.")
            db.add(models.Alert(kind="parity", ref_id=ref, level="info", message=msg[:1000]))
            try:
                from . import diag
                diag.record("parity", f, v, msg, {"campaign": camp[:80], "advertiser_id": getattr(acct, "advertiser_id", "")})
            except Exception:      # noqa: BLE001
                pass
            new += 1
        return new
    except Exception:      # noqa: BLE001 — a watch must never break the sync
        log.exception("parity observe failed")
        return 0
