"""Performance — live-polling metrics dashboard.

The page polls /performance/data with an overlap-guarded auto-refresh
(AbortController on the client; the server never blanks partial state), and an
optional audio ding fires when the conversion count rises. Numbers come from
the same truth as the P&L: SpendSnapshot (TikTok) + PostbackEvent (Glitchy).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import live_log, pnl_data, queries, timeutil
from ..database import get_db

router = APIRouter()


@router.get("/performance")
def performance_page(request: Request):
    """Merged into Home (live KPIs + feed) — keep the old URL working."""
    return RedirectResponse("/", status_code=303)


@router.get("/performance/data")
def performance_data(request: Request, db: Session = Depends(get_db)):
    """JSON the poller consumes: today's KPI totals + recent live events."""
    start_utc, end_utc = timeutil.range_bounds("today")
    totals = pnl_data.overall_totals(db, start_utc, end_utc)
    last_id = int(request.query_params.get("last_id", 0) or 0)
    events = live_log.since(last_id)
    return {
        "spend": round(totals["spend"], 2),
        "revenue": round(totals["revenue"], 2),
        "profit": round(totals["profit"], 2),
        "roas": round(totals["roas"], 2),
        "conversions": totals["conversions"],
        "clicks": totals["clicks"],
        "events": events,
        "synced_ago": queries.campaigns_synced_ago(db),
    }
