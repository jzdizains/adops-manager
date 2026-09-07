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
from ..settings_store import get_settings
from ..templating import render

router = APIRouter()

# range key → (label, days back from yesterday); today/yesterday/custom are special
RANGES = {"today": ("Today (partial)", 0), "yesterday": ("Yesterday", 1), "3": ("Last 3 days", 3),
          "7": ("Last 7 days", 7), "14": ("Last 14 days", 14), "30": ("Last 30 days", 30), "custom": ("Custom…", 0)}
MAX_SPAN = 45     # the store keeps 45 days


def _parse_day(v: str) -> date | None:
    try:
        return date.fromisoformat((v or "").strip()[:10])
    except ValueError:
        return None


def resolve_range(rng: str, with_today: bool, today: date, start_q: str = "", end_q: str = "") -> tuple[date, date, bool]:
    """(start, end, includes_today) for the picker value. Audience data lags
    10–12 h, so multi-day ranges end yesterday unless 'include today' is on."""
    if rng == "today":
        return today, today, True
    if rng == "yesterday":
        y = today - timedelta(days=1)
        return y, y, False
    if rng == "custom":
        s_, e_ = _parse_day(start_q), _parse_day(end_q)
        if not s_ or not e_ or e_ < s_:
            rng = "7"
        else:
            e_ = min(e_, today)
            s_ = max(s_, e_ - timedelta(days=MAX_SPAN - 1))
            return s_, e_, e_ >= today
    n = RANGES.get(rng, RANGES["7"])[1] or 7
    end = today if with_today else today - timedelta(days=1)
    start = (today - timedelta(days=1)) - timedelta(days=n - 1)
    return start, end, with_today
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
    start_q, end_q = qp.get("start", ""), qp.get("end", "")
    metric = qp.get("metric", "spend") if qp.get("metric", "spend") in METRICS else "spend"
    bc = qp.get("bc", "").strip()
    account = qp.get("account", "").strip()
    camp = qp.get("camp", "").strip()
    with_today = qp.get("today", "") == "1"
    today = date.fromisoformat(timeutil.local_date_str())
    start, end, with_today = resolve_range(rng, with_today, today, start_q, end_q)
    if rng == "custom" and (start, end) == resolve_range("7", with_today, today)[:2]:
        rng = "7"           # unparseable custom dates → the default
    s, e = start.isoformat(), end.isoformat()
    ids = _accounts_for(db, bc, account)
    regions = aud.region_names(db)
    sections = []
    for key, title in SECTIONS:
        rows = aud.breakdown(db, key, s, e, ids, camp, regions)
        sections.append({"key": key, "title": title, "rows": rows[:40], "n": len(rows),
                         "spend": sum(r["spend"] for r in rows)})
    # the heatmap covers the same days; for ranges ending yesterday it still
    # adds today's hours (they are near real-time, unlike the breakdowns)
    heat_end = today.isoformat() if rng not in ("yesterday", "custom") or with_today else e
    heat = aud.heatmap(db, s, heat_end, ids, camp, metric)
    bcs = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    accounts = db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
    accounts = accounts.order_by(models.AdAccount.advertiser_name).all()
    if bc and bc != "none":
        accounts = [a for a in accounts if a.owner_bc_id == bc]
    return render(request, "audience.html", {
        "title": "Audience", "rng": rng, "ranges": RANGES, "metric": metric, "metrics": METRICS,
        "start_q": start_q if rng == "custom" else s, "end_q": end_q if rng == "custom" else e,
        "bc": bc, "bcs": sorted(bcs.items(), key=lambda kv: kv[1].lower()), "account": account,
        "accounts": accounts, "camp": camp, "start": s, "end": e, "today": today.isoformat(),
        "sections": sections, "heat": heat, "coverage": aud.coverage(db),
        "has_data": any(sec["n"] for sec in sections) or heat["max"] > 0,
        "filtered": bool(bc or account or camp), "with_today": with_today,
        "hours_every": int(get_settings(db).get("audience_hours_every_min") or 10),
        "breakdown_every": int(get_settings(db).get("audience_breakdown_every_min") or 60),
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
