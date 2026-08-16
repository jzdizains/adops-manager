"""Performance — live-polling metrics dashboard.

The page polls /performance/data with an overlap-guarded auto-refresh
(AbortController on the client; the server never blanks partial state), and an
optional audio ding fires when the conversion count rises.
Also serves the Revenue page (network revenue from ConversionSample rows).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import func

from .. import live_log, models, queries, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/performance")
def performance_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "performance.html", {"title": "Performance"})


@router.get("/performance/data")
def performance_data(request: Request, db: Session = Depends(get_db)):
    """JSON the poller consumes: KPI totals + recent live events."""
    start_utc, end_utc = timeutil.range_bounds("today")
    rev = queries.revenue_between(db, start_utc, end_utc)
    spend = queries.spend_today(db)
    clicks = 0.0  # clicks ride the campaign sync; kept 0 until reporting sync adds them
    last_id = int(request.query_params.get("last_id", 0) or 0)
    events = live_log.since(last_id)
    return {
        "spend": round(spend, 2),
        "revenue": round(rev["revenue"], 2),
        "profit": round(rev["revenue"] - spend, 2),
        "roas": round(rev["revenue"] / spend, 2) if spend > 0 else 0,
        "conversions": rev["conversions"],
        "clicks": clicks,
        "events": events,
        "synced_ago": queries.campaigns_synced_ago(db),
    }


@router.get("/revenue")
def revenue_page(request: Request, db: Session = Depends(get_db)):
    range_key = request.query_params.get("range", "today")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    start_utc, end_utc = timeutil.range_bounds(range_key, start, end)
    by_network = (
        db.query(models.ConversionSample.network,
                 func.sum(models.ConversionSample.revenue).label("revenue"),
                 func.sum(models.ConversionSample.conversions).label("conversions"))
        .filter(models.ConversionSample.sampled_at >= start_utc.replace(tzinfo=None),
                models.ConversionSample.sampled_at < end_utc.replace(tzinfo=None))
        .group_by(models.ConversionSample.network).all())
    samples = (db.query(models.ConversionSample)
               .filter(models.ConversionSample.sampled_at >= start_utc.replace(tzinfo=None),
                       models.ConversionSample.sampled_at < end_utc.replace(tzinfo=None))
               .order_by(models.ConversionSample.sampled_at.desc()).limit(100).all())
    total_rev = sum(float(r.revenue or 0) for r in by_network)
    total_conv = sum(int(r.conversions or 0) for r in by_network)
    return render(request, "revenue.html", {
        "title": "Revenue", "range_key": range_key, "start": start or "", "end": end or "",
        "by_network": by_network, "samples": samples,
        "total_rev": total_rev, "total_conv": total_conv,
    })
