"""In-app alerts: feeds the bell dropdown and the Overview banner."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import balances, models
from ..database import get_db

router = APIRouter()


def _alert_href(a: models.Alert) -> str:
    """Where clicking an alert should take the operator."""
    if a.kind == "bc_low_balance" and a.ref_id:
        return balances.bc_portal_url(a.ref_id)      # straight to the BC (new tab)
    if a.kind == "rule_action":
        return "/monitor?view=automation"
    if a.kind == "account_error" or a.kind == "inventory_low":
        return "/monitor"
    return ""


_BELL_CACHE: dict = {}               # view key -> (expires_at, payload) — one build shared by every open tab of that view
_BELL_TTL_S = 20


def bell_cache_clear() -> None:
    _BELL_CACHE.clear()


@router.get("/alerts/data")
def alerts_data(request: Request, db: Session = Depends(get_db)):
    """Bell poller: the UNIFIED inbox (alerts + issues + failed launches +
    queue + cooldown + connection), not just Alert rows. Every open tab polls
    this each minute, so the (10-query) build is shared for a few seconds and
    dropped as soon as something is acknowledged."""
    import time as _time
    from .. import inbox as inbox_mod, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    key = (sc.mode, sc.user_id)
    hit = _BELL_CACHE.get(key)
    if hit and hit[0] > _time.time():
        return hit[1]
    items = inbox_mod.build(db, sc)
    counts = inbox_mod.counts(items)
    payload = {"count": counts["total"], "counts": counts,
               "alerts": [inbox_mod.serialize(i) for i in items[:8]]}
    if len(_BELL_CACHE) > 100:
        _BELL_CACHE.clear()
    _BELL_CACHE[key] = (_time.time() + _BELL_TTL_S, payload)
    return payload


@router.post("/alerts/{alert_id}/ack")
def acknowledge(request: Request, alert_id: int, db: Session = Depends(get_db)):
    from .. import inbox as inbox_mod, scope as scope_mod
    a = db.get(models.Alert, alert_id)
    if a and inbox_mod.alert_visible(a, scope_mod.for_request(request, db)):
        a.acknowledged = True
        db.commit()
    bell_cache_clear()
    return {"ok": True}


@router.post("/alerts/ack-all")
def acknowledge_all(request: Request, db: Session = Depends(get_db)):
    """Acknowledge what THIS person sees — not every workspace's alerts."""
    from .. import inbox as inbox_mod, scope as scope_mod
    for a in inbox_mod.visible_unacked(db, scope_mod.for_request(request, db)):
        a.acknowledged = True
    db.commit()
    bell_cache_clear()
    return {"ok": True}


@router.post("/balances/sync")
def sync_now(db: Session = Depends(get_db)):
    balances.run_sweep(db)
    bell_cache_clear()
    return RedirectResponse("/monitor", status_code=303)
