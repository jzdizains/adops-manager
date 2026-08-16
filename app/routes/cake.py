"""CAKE + Taprain network pages — same shape as Everflow."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import cake_api, live_log, models, taprain_api, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/cake")
def cake_page(request: Request, db: Session = Depends(get_db)):
    samples = (db.query(models.ConversionSample).filter_by(network="cake")
               .order_by(models.ConversionSample.sampled_at.desc()).limit(50).all())
    return render(request, "network.html", {
        "title": "CAKE", "network": "cake",
        "configured": cake_api.configured(), "samples": samples,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/cake/pull")
def cake_pull(db: Session = Depends(get_db)):
    if not cake_api.configured():
        return RedirectResponse("/cake?err=Set+CAKE_API_URL+and+CAKE_API_KEY", status_code=303)
    local_today = timeutil.now_local().strftime("%m/%d/%Y")
    try:
        rows = cake_api.pull_samples(local_today, local_today)
    except Exception as e:
        return RedirectResponse(f"/cake?err={str(e)[:150]}", status_code=303)
    _store(db, "cake", rows)
    return RedirectResponse(f"/cake?ok=pulled+{len(rows)}+rows", status_code=303)


@router.get("/taprain")
def taprain_page(request: Request, db: Session = Depends(get_db)):
    samples = (db.query(models.ConversionSample).filter_by(network="taprain")
               .order_by(models.ConversionSample.sampled_at.desc()).limit(50).all())
    return render(request, "network.html", {
        "title": "Taprain", "network": "taprain",
        "configured": taprain_api.configured(), "samples": samples,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/taprain/pull")
def taprain_pull(db: Session = Depends(get_db)):
    if not taprain_api.configured():
        return RedirectResponse("/taprain?err=Set+TAPRAIN_API_KEY", status_code=303)
    today = timeutil.local_date_str()
    try:
        rows = taprain_api.pull_samples(today, today)
    except Exception as e:
        return RedirectResponse(f"/taprain?err={str(e)[:150]}", status_code=303)
    _store(db, "taprain", rows)
    return RedirectResponse(f"/taprain?ok=pulled+{len(rows)}+rows", status_code=303)


def _store(db: Session, network: str, rows: list[dict]):
    start, end = timeutil.range_bounds("today")
    total = 0
    for r in rows:
        db.add(models.ConversionSample(network=network, sub_id=r["sub_id"],
                                       conversions=r["conversions"], revenue=r["revenue"],
                                       period_start=start.replace(tzinfo=None),
                                       period_end=end.replace(tzinfo=None)))
        total += r["conversions"]
    db.commit()
    if total:
        live_log.push("conversion", f"{network}: {total} conversion(s) sampled")
