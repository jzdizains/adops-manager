"""/audience — who the ads reach: hour × weekday heatmap, age × gender,
gender, age, country, state, OS, device brand, placement, network, language.
Reads the AudienceStat store (filled daily by the audience_sync job)."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import audience as aud
from .. import models, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()

RANGES = {"7": 7, "14": 14, "30": 30}
METRICS = {"spend": "Spend", "impressions": "Impressions", "clicks": "Clicks", "conversions": "Conversions"}
SECTIONS = [
    ("age_gender", "Age × gender"), ("gender", "Gender"), ("age", "Age"),
    ("country", "Country"), ("province", "State / region"), ("platform", "Operating system"),
    ("device_brand", "Device brand"), ("placement", "Placement"), ("ac", "Network"), ("language", "Language"),
]


def _accounts_for(db: Session, bc: str, account: str) -> list[str] | None:
    """advertiser ids matching the BC / account filter; None = no filter."""
    if account:
        return [account]
    if bc:
        q = db.query(models.AdAccount.advertiser_id)
        if bc == "none":
            q = q.filter((models.AdAccount.owner_bc_id == "") | (models.AdAccount.owner_bc_id.is_(None)))
        else:
            q = q.filter(models.AdAccount.owner_bc_id == bc)
        return [r[0] for r in q.all()]
    return None


@router.get("/audience")
def audience_page(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    rng = qp.get("range", "7") if qp.get("range", "7") in RANGES else "7"
    metric = qp.get("metric", "spend") if qp.get("metric", "spend") in METRICS else "spend"
    bc = qp.get("bc", "").strip()
    account = qp.get("account", "").strip()
    camp = qp.get("camp", "").strip()
    today = date.fromisoformat(timeutil.local_date_str())
    # audience data lags 10–12 h, so the range ends yesterday; the heatmap adds today's hours
    end = today - timedelta(days=1)
    start = end - timedelta(days=RANGES[rng] - 1)
    s, e = start.isoformat(), end.isoformat()
    ids = _accounts_for(db, bc, account)
    regions = aud.region_names(db)
    sections = []
    for key, title in SECTIONS:
        rows = aud.breakdown(db, key, s, e, ids, camp, regions)
        sections.append({"key": key, "title": title, "rows": rows[:40], "n": len(rows),
                         "spend": sum(r["spend"] for r in rows)})
    heat = aud.heatmap(db, s, today.isoformat(), ids, camp, metric)
    bcs = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    accounts = db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
    accounts = accounts.order_by(models.AdAccount.advertiser_name).all()
    if bc and bc != "none":
        accounts = [a for a in accounts if a.owner_bc_id == bc]
    return render(request, "audience.html", {
        "title": "Audience", "rng": rng, "ranges": RANGES, "metric": metric, "metrics": METRICS,
        "bc": bc, "bcs": sorted(bcs.items(), key=lambda kv: kv[1].lower()), "account": account,
        "accounts": accounts, "camp": camp, "start": s, "end": e, "today": today.isoformat(),
        "sections": sections, "heat": heat, "coverage": aud.coverage(db),
        "has_data": any(sec["n"] for sec in sections) or heat["max"] > 0,
        "filtered": bool(bc or account or camp),
    })


@router.post("/audience/sync")
def sync_now(days: str = Form("2"), db: Session = Depends(get_db)):
    """Queue a refresh: today's hours + the last N full days of breakdowns."""
    from .. import jobs, queries
    if not queries.any_access_token(db):
        return RedirectResponse("/audience?err=" + quote("Connect TikTok first."), status_code=303)
    try:
        n = max(1, min(int(days), 30))
    except ValueError:
        n = 2
    today = date.fromisoformat(timeutil.local_date_str())
    full = [(today - timedelta(days=i)).isoformat() for i in range(1, n + 1)]
    payload = {"days": {"hours": [today.isoformat()] + full, "audience": full}}
    job, created = jobs.enqueue_once(db, "audience_sync", f"Refresh audience breakdowns ({n} day{'s' if n != 1 else ''})",
                                     payload, href="/audience")
    if not created:
        return RedirectResponse("/audience?ok=" + quote(f"A refresh is already {job.status}"
                                                        + (f" · {job.progress}" if job.progress else "")
                                                        + " — you can stop it on the Jobs page."), status_code=303)
    return RedirectResponse("/audience?ok=" + quote(
        f"Refreshing {n} day(s) in the background — every account, about {n * 9} TikTok calls per active account; "
        "you'll get a notification."), status_code=303)
