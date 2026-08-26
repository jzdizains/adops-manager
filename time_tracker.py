"""VA Dash (/time/) — clock in/out/break, live timer, today's totals, weekly bar
chart, editable hourly rate. The rate is STAMPED on the session at clock-in so
a later rate change never rewrites past pay. /time/history is a per-day
timesheet with frozen per-day rates and mark-as-paid."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _current_rate(db: Session) -> float:
    row = db.query(models.TimeSetting).first()
    return float(row.hourly_rate) if row else 0.0


def _open_session(db: Session) -> models.TimeSession | None:
    return (db.query(models.TimeSession)
            .filter(models.TimeSession.clock_out.is_(None))
            .order_by(models.TimeSession.clock_in.desc()).first())


def _open_break(db: Session, session: models.TimeSession) -> models.TimeBreak | None:
    return (db.query(models.TimeBreak)
            .filter_by(session_id=session.id)
            .filter(models.TimeBreak.break_end.is_(None)).first())


def session_seconds(s: models.TimeSession) -> tuple[float, float]:
    """(worked_seconds, break_seconds) — open segments count up to now."""
    now = _naive_utc(timeutil.now_utc())
    end = s.clock_out or now
    total = (end - s.clock_in).total_seconds()
    brk = 0.0
    for b in s.breaks:
        b_end = b.break_end or (end if s.clock_out else now)
        brk += max((b_end - b.break_start).total_seconds(), 0)
    return max(total - brk, 0), brk


@router.get("/time/")
def dash(request: Request, db: Session = Depends(get_db)):
    open_s = _open_session(db)
    on_break = bool(open_s and _open_break(db, open_s))
    rate = _current_rate(db)

    day_start = timeutil.local_midnight_utc(0).replace(tzinfo=None)
    todays = (db.query(models.TimeSession)
              .filter(models.TimeSession.clock_in >= day_start).all())
    today_secs = sum(session_seconds(s)[0] for s in todays)
    today_pay = sum(session_seconds(s)[0] / 3600 * s.hourly_rate for s in todays)

    # weekly bar chart: last 7 local days
    week = []
    for offset in range(-6, 1):
        d0 = timeutil.local_midnight_utc(offset).replace(tzinfo=None)
        d1 = timeutil.local_midnight_utc(offset + 1).replace(tzinfo=None)
        sessions = (db.query(models.TimeSession)
                    .filter(models.TimeSession.clock_in >= d0,
                            models.TimeSession.clock_in < d1).all())
        secs = sum(session_seconds(s)[0] for s in sessions)
        week.append({"label": timeutil.fmt_local(d0.replace(tzinfo=timezone.utc), "%a"),
                     "hours": round(secs / 3600, 2)})

    elapsed = 0
    if open_s:
        elapsed = int(session_seconds(open_s)[0])
    return render(request, "time_dash.html", {
        "title": "VA Dash", "open_session": open_s, "on_break": on_break,
        "rate": rate, "today_hours": round(today_secs / 3600, 2),
        "today_pay": round(today_pay, 2), "week": week, "elapsed": elapsed,
    })


@router.post("/time/clock-in")
def clock_in(db: Session = Depends(get_db)):
    if not _open_session(db):
        db.add(models.TimeSession(hourly_rate=_current_rate(db)))  # rate FROZEN here
        db.commit()
    return RedirectResponse("/time/", status_code=303)


@router.post("/time/clock-out")
def clock_out(db: Session = Depends(get_db)):
    s = _open_session(db)
    if s:
        b = _open_break(db, s)
        now = _naive_utc(timeutil.now_utc())
        if b:
            b.break_end = now
        s.clock_out = now
        db.commit()
    return RedirectResponse("/time/", status_code=303)


@router.post("/time/break")
def toggle_break(db: Session = Depends(get_db)):
    s = _open_session(db)
    if s:
        b = _open_break(db, s)
        now = _naive_utc(timeutil.now_utc())
        if b:
            b.break_end = now
        else:
            db.add(models.TimeBreak(session_id=s.id, break_start=now))
        db.commit()
    return RedirectResponse("/time/", status_code=303)


@router.post("/time/rate")
def set_rate(hourly_rate: float = Form(...), db: Session = Depends(get_db)):
    row = db.query(models.TimeSetting).first()
    if not row:
        row = models.TimeSetting()
        db.add(row)
    row.hourly_rate = float(hourly_rate)
    db.commit()
    return RedirectResponse("/time/", status_code=303)


@router.get("/time/history")
def history(request: Request, db: Session = Depends(get_db)):
    sessions = (db.query(models.TimeSession)
                .filter(models.TimeSession.clock_out.isnot(None))
                .order_by(models.TimeSession.clock_in.desc()).limit(500).all())
    days: dict[str, dict] = defaultdict(lambda: {"secs": 0.0, "pay": 0.0, "sessions": [], "paid": True})
    for s in sessions:
        key = timeutil.local_date_str(s.clock_in.replace(tzinfo=timezone.utc))
        worked, _ = session_seconds(s)
        days[key]["secs"] += worked
        days[key]["pay"] += worked / 3600 * s.hourly_rate   # frozen per-session rate
        days[key]["sessions"].append(s)
        days[key]["paid"] = days[key]["paid"] and s.paid
    rows = [{"day": k, "hours": round(v["secs"] / 3600, 2), "pay": round(v["pay"], 2),
             "paid": v["paid"], "count": len(v["sessions"]),
             "ids": ",".join(str(s.id) for s in v["sessions"])}
            for k, v in sorted(days.items(), reverse=True)]
    return render(request, "time_history.html", {"rows": rows, "title": "Timesheet"})


@router.post("/time/mark-paid")
def mark_paid(ids: str = Form(...), db: Session = Depends(get_db)):
    for sid in ids.split(","):
        if sid.strip().isdigit():
            s = db.get(models.TimeSession, int(sid))
            if s:
                s.paid = True
    db.commit()
    return RedirectResponse("/time/history", status_code=303)
