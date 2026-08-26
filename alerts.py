"""In-app alerts: feeds the bell dropdown and the Overview banner."""
from __future__ import annotations

from fastapi import APIRouter, Depends
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
        return "/automation"
    if a.kind == "account_error" or a.kind == "inventory_low":
        return "/monitor"
    return ""


@router.get("/alerts/data")
def alerts_data(db: Session = Depends(get_db)):
    rows = balances.unacknowledged(db)
    return {"count": len(rows), "alerts": [
        {"id": a.id, "kind": a.kind, "level": a.level, "message": a.message,
         "href": _alert_href(a),
         "external": a.kind == "bc_low_balance",
         "at": (a.created_at.isoformat() + "Z") if a.created_at else ""}
        for a in rows]}


@router.post("/alerts/{alert_id}/ack")
def acknowledge(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(models.Alert, alert_id)
    if a:
        a.acknowledged = True
        db.commit()
    return {"ok": True}


@router.post("/alerts/ack-all")
def acknowledge_all(db: Session = Depends(get_db)):
    db.query(models.Alert).filter_by(acknowledged=False).update({"acknowledged": True})
    db.commit()
    return {"ok": True}


@router.post("/balances/sync")
def sync_now(db: Session = Depends(get_db)):
    balances.run_sweep(db)
    return RedirectResponse("/monitor", status_code=303)
