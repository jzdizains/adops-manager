"""/jobs — background actions: the notification feed every page polls, and
the history page."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import jobs, models
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/jobs/data")
def jobs_data(db: Session = Depends(get_db)):
    """Unseen finished jobs (→ notifications) + what's running. Marks the
    returned finished jobs as seen so each is announced once."""
    done = (db.query(models.Job)
            .filter(models.Job.status.in_(("done", "error")), models.Job.seen == False)  # noqa: E712
            .order_by(models.Job.finished_at).limit(20).all())
    items = []
    for j in done:
        items.append({"id": j.id, "kind": j.kind, "title": j.title, "status": j.status,
                      "detail": j.detail or "", "href": j.href or "/jobs"})
        j.seen = True
    running = (db.query(models.Job).filter(models.Job.status.in_(("queued", "running")))
               .order_by(models.Job.id).all())
    db.commit()
    return JSONResponse({"done": items,
                         "running": [{"id": j.id, "kind": j.kind, "title": j.title, "status": j.status,
                                      "progress": j.progress or ""} for j in running]})


@router.get("/jobs")
def jobs_page(request: Request, db: Session = Depends(get_db)):
    rows = db.query(models.Job).order_by(models.Job.id.desc()).limit(150).all()
    return render(request, "jobs.html", {"title": "Background jobs", "rows": rows, "summary": jobs.summary(db)})
