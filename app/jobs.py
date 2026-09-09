"""Background jobs — every slow TikTok action runs here, not in the request.

A route validates its input, calls enqueue(kind, title, payload, href) and
redirects immediately. A dedicated worker thread (started with the app) picks
queued jobs up within a second, runs the registered handler with its own DB
session, and stores the outcome. Every open page polls /jobs/data and shows a
clickable notification bottom-right when a job finishes (10 s, click → href);
the Jobs page keeps the history.

Handlers: @handler("kind") def fn(db, payload) -> dict(ok=bool, detail=str,
href=str|None). Raising is caught and stored as an error. A handler may call
progress(db, job, "3 of 12") to update the running label.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from . import models

log = logging.getLogger("adops.jobs")

HANDLERS: dict[str, Callable] = {}
_wake = threading.Event()
_started = False
_lock = threading.Lock()
_current: dict = {}      # job id → job (for progress updates)

# Two lanes, two worker threads: long sweeps over every account (minutes) must
# never make a launch or a bid change wait behind them.
SLOW_KINDS = {"issues_scan", "appeals_refresh", "pixels_sync", "pixel_link_all", "audience_sync", "music_sync", "identities_sync", "spark_authorize"}
LANES = ("fast", "slow")
CANCELLED = "cancelled"


def lane(kind: str) -> str:
    return "slow" if kind in SLOW_KINDS else "fast"


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enqueue(db: Session, kind: str, title: str, payload: dict | None = None, href: str = "", quiet: bool = False) -> models.Job:
    """quiet=True for jobs the sweep schedules on its own (audience / hourly refreshes, monthly
    music sync): they still show on the Jobs page but never pop a notification unless they fail —
    otherwise a user logging in after a night away is greeted by a wall of "refreshed" toasts."""
    job = models.Job(kind=kind, title=title[:200], payload=json.dumps(payload or {}, default=str),
                     href=href or "", status="queued", cancel_requested=False, quiet=quiet)
    db.add(job)
    db.commit()
    if _inline():
        run_job(db, job)          # no worker thread (tests / ADOPS_DISABLE_BG) → do it now
        return job
    _wake.set()
    return job


def pending(db: Session, kind: str) -> models.Job | None:
    """The queued/running job of this kind, if any — so a button pressed twice
    doesn't stack the same sweep up behind itself."""
    return (db.query(models.Job).filter(models.Job.kind == kind, models.Job.status.in_(("queued", "running")))
            .order_by(models.Job.id).first())


def enqueue_once(db: Session, kind: str, title: str, payload: dict | None = None,
                 href: str = "", quiet: bool = False) -> tuple[models.Job, bool]:
    """enqueue() unless the same kind is already queued or running.
    Returns (job, created)."""
    existing = pending(db, kind)
    if existing:
        return existing, False
    return enqueue(db, kind, title, payload, href, quiet=quiet), True


def cancel(db: Session, job_id: int) -> tuple[bool, str]:
    """Queued → dropped before it starts. Running → asks the handler to stop
    at its next checkpoint (long sweeps check between accounts); a handler
    that can't stop early finishes normally."""
    job = db.get(models.Job, job_id)
    if not job:
        return False, "That job no longer exists."
    if job.status == "queued":
        job.status = CANCELLED
        job.detail = "removed from the queue before it started"
        job.finished_at = _now()
        job.seen = True                 # nothing to announce
        db.commit()
        return True, f"Removed “{job.title}” from the queue."
    if job.status == "running":
        if job.cancel_requested:
            return True, f"“{job.title}” is already being stopped — it ends at its next checkpoint."
        job.cancel_requested = True
        db.commit()
        return True, f"Stopping “{job.title}” — it ends at its next checkpoint (long scans check after every account)."
    return False, f"“{job.title}” already finished."


def cancel_queued(db: Session) -> int:
    """Drop everything still waiting (running jobs are left alone)."""
    n = 0
    for job in db.query(models.Job).filter(models.Job.status == "queued").all():
        job.status = CANCELLED
        job.detail = "removed from the queue before it started"
        job.finished_at = _now()
        job.seen = True
        n += 1
    db.commit()
    return n


def should_stop(db: Session, job: models.Job | None) -> bool:
    """Handlers call this at checkpoints; the flag is set from another session."""
    if job is None:
        return False
    try:
        db.expire(job, ["cancel_requested"])
        return bool(job.cancel_requested)
    except Exception:  # noqa: BLE001 — never let a cancel check break a job
        return False


def _inline() -> bool:
    """Run jobs synchronously when the worker isn't running — ADOPS_DISABLE_BG=1
    (tests, one-off scripts) — unless ADOPS_JOBS_INLINE=0 asks for real queuing."""
    v = os.environ.get("ADOPS_JOBS_INLINE")
    if v is not None:
        return v == "1"
    return os.environ.get("ADOPS_DISABLE_BG") == "1"


def progress(db: Session, job: models.Job, text: str) -> None:
    job.progress = text[:80]
    db.commit()


def run_job(db: Session, job: models.Job) -> None:
    if job.status == CANCELLED:        # cancelled between being picked and started
        return
    fn = HANDLERS.get(job.kind)
    job.status = "running"
    job.started_at = _now()
    db.commit()
    try:
        if not fn:
            raise RuntimeError(f"no handler for job kind {job.kind!r}")
        payload = json.loads(job.payload or "{}")
        res = fn(db, payload, job) or {}
        job.status = "done" if res.get("ok", True) else "error"
        job.detail = str(res.get("detail") or "")[:600]
        if res.get("href"):
            job.href = str(res["href"])
    except Exception as e:  # noqa: BLE001 — a job must never kill the worker
        log.exception("job %s (%s) failed", job.id, job.kind)
        db.rollback()
        job = db.merge(job)
        job.status = "error"
        job.detail = f"{type(e).__name__}: {str(e)[:400]}"
    job.finished_at = _now()
    job.progress = ""
    if job.quiet and job.status == "done":
        job.seen = True                # scheduled housekeeping that worked: no toast, just the Jobs list
    db.commit()


def run_pending(db: Session, limit: int = 20, which: str | None = None) -> int:
    """Run queued jobs oldest-first (used by the workers, and directly by tests).
    which = "fast" / "slow" restricts to that lane; None runs everything."""
    n = 0
    for _ in range(limit):
        q = db.query(models.Job).filter(models.Job.status == "queued")
        if which == "slow":
            q = q.filter(models.Job.kind.in_(SLOW_KINDS))
        elif which == "fast":
            q = q.filter(~models.Job.kind.in_(SLOW_KINDS))
        job = q.order_by(models.Job.id).first()
        if not job:
            break
        run_job(db, job)
        n += 1
    return n


def prune(db: Session, keep_days: int = 14) -> int:
    cutoff = _now() - timedelta(days=keep_days)
    return db.query(models.Job).filter(models.Job.created_at < cutoff).delete()


def _loop(which: str):
    from .database import SessionLocal
    while True:
        _wake.wait(timeout=5.0)
        # (not cleared here: both lanes wake on the same event and each simply
        #  finds nothing to do within a few ms; the 5 s timeout covers the rest)
        db = SessionLocal()
        try:
            run_pending(db, which=which)
        except Exception:  # noqa: BLE001
            log.exception("jobs worker (%s) sweep failed", which)
        finally:
            db.close()
        _wake.clear()


def start() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    for which in LANES:
        threading.Thread(target=_loop, args=(which,), name=f"adops-jobs-{which}", daemon=True).start()


def recover(db: Session) -> int:
    """At boot: jobs still 'running' or 'queued' from a previous process. Queued
    ones will run; 'running' ones died mid-way — mark them so nobody waits."""
    n = 0
    for j in db.query(models.Job).filter(models.Job.status == "running").all():
        j.status = "error"
        j.detail = "interrupted by a restart — check the result page before retrying"
        j.finished_at = _now()
        n += 1
    db.commit()
    return n


def summary(db: Session) -> dict:
    running = db.query(models.Job).filter(models.Job.status == "running").count()
    queued = db.query(models.Job).filter(models.Job.status == "queued").count()
    return {"running": running, "queued": queued}
