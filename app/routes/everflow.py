"""Everflow network page — pull conversion/revenue samples into ConversionSample."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import everflow_api, live_log, models, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/everflow")
def page(request: Request, db: Session = Depends(get_db)):
    samples = (db.query(models.ConversionSample).filter_by(network="everflow")
               .order_by(models.ConversionSample.sampled_at.desc()).limit(50).all())
    return render(request, "network.html", {
        "title": "Everflow", "network": "everflow",
        "configured": everflow_api.configured(), "samples": samples,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/everflow/pull")
def pull(db: Session = Depends(get_db)):
    if not everflow_api.configured():
        return RedirectResponse("/everflow?err=Set+EVERFLOW_API_KEY+first", status_code=303)
    today = timeutil.local_date_str()
    try:
        rows = everflow_api.pull_samples(today, today)
    except Exception as e:
        return RedirectResponse(f"/everflow?err={str(e)[:150]}", status_code=303)
    start, end = timeutil.range_bounds("today")
    new_conversions = 0
    for r in rows:
        db.add(models.ConversionSample(network="everflow", sub_id=r["sub_id"],
                                       conversions=r["conversions"], revenue=r["revenue"],
                                       period_start=start.replace(tzinfo=None),
                                       period_end=end.replace(tzinfo=None)))
        new_conversions += r["conversions"]
    db.commit()
    if new_conversions:
        live_log.push("conversion", f"Everflow: {new_conversions} conversion(s) sampled")
    return RedirectResponse(f"/everflow?ok=pulled+{len(rows)}+rows", status_code=303)
