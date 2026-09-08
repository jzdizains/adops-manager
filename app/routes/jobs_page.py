"""/jobs — background actions: the notification feed every page polls, and
the history page."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import jobs, models
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/jobs/data")
def jobs_data(request: Request, db: Session = Depends(get_db)):
    """Unseen finished jobs (→ notifications) + what's running. Marks the
    returned finished jobs as seen so each is announced once — unless
    ?peek=1 (the Jobs page polling), which must not eat the notifications."""
    from sqlalchemy import func
    peek = request.query_params.get("peek") == "1"
    items = []
    if not peek:
        done = (db.query(models.Job)
                .filter(models.Job.status.in_(("done", "error")), models.Job.seen == False)  # noqa: E712
                .order_by(models.Job.finished_at).limit(20).all())
        for j in done:
            items.append({"id": j.id, "kind": j.kind, "title": j.title, "status": j.status,
                          "detail": j.detail or "", "href": j.href or "/jobs"})
            j.seen = True
    running = (db.query(models.Job).filter(models.Job.status.in_(("queued", "running")))
               .order_by(models.Job.id).all())
    done_count = db.query(func.count(models.Job.id)).filter(models.Job.status.in_(("done", "error", "cancelled"))).scalar() or 0
    if items:
        db.commit()      # only a write when something was marked seen — every tab polls this, and SQLite has one writer
    return JSONResponse({"done": items, "done_count": done_count,
                         "running": [{"id": j.id, "kind": j.kind, "title": j.title, "status": j.status,
                                      "progress": j.progress or ""} for j in running]})


@router.get("/jobs")
def jobs_page(request: Request, db: Session = Depends(get_db)):
    from .. import timeutil
    rows = db.query(models.Job).order_by(models.Job.id.desc()).limit(150).all()
    day_start = timeutil.local_midnight_utc(0).replace(tzinfo=None)
    today = [j for j in rows if j.finished_at and j.finished_at >= day_start]
    return render(request, "jobs.html", {"title": "Jobs", "rows": rows, "summary": jobs.summary(db), "slow_kinds": jobs.SLOW_KINDS,
                                         "done_today": sum(1 for j in today if j.status == "done"), "failed_today": sum(1 for j in today if j.status == "error")})


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/jobs"


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int, next: str = Form("/jobs"), db: Session = Depends(get_db)):
    """Queued → removed from the queue. Running → asked to stop at its next checkpoint."""
    ok, msg = jobs.cancel(db, job_id)
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": ok, "msg": msg})
    return RedirectResponse(_safe_next(next) + ("?ok=" if ok else "?err=") + quote(msg), status_code=303)


@router.post("/jobs/cancel-queued")
def cancel_queued(request: Request, next: str = Form("/jobs"), db: Session = Depends(get_db)):
    n = jobs.cancel_queued(db)
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "n": n})
    return RedirectResponse(_safe_next(next) + "?ok=" + quote(f"Removed {n} queued job(s)." if n else "The queue was already empty."),
                            status_code=303)
