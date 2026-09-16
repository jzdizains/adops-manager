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

# Three lanes (v116): long sweeps over every account (minutes) must never make a bid
# change wait behind them, and with several users launching at once, launches get a
# lane of their own with LAUNCH_WORKERS threads — one user's ten-account batch no
# longer holds up another user's launch, and a manual campaign sync (minutes over
# hundreds of accounts) never sits in front of a launch either.
SLOW_KINDS = {"instant_page_build", "instant_page_clone_all", "lead_form_clone_all", "issues_scan", "appeals_refresh", "pixels_sync", "pixel_link_all", "audience_sync", "music_sync", "identities_sync", "spark_authorize", "bc_assets_scan", "bc_assets_wire", "bc_assets_connect", "adgroup_duplicate"}
LAUNCH_KINDS = {"launch"}
SYNC_KINDS = {"status_sync"}      # the manual campaign sync: its own lane, never in front of a launch or behind a scan
LANES = ("fast", "slow", "launch", "sync")
LAUNCH_WORKERS = 2               # parallel launch threads (each user's launches use their own TikTok login)
CANCELLED = "cancelled"


def lane(kind: str) -> str:
    if kind in LAUNCH_KINDS:
        return "launch"
    if kind in SYNC_KINDS:
        return "sync"
    return "slow" if kind in SLOW_KINDS else "fast"


def _launched_by(job) -> str:
    """Who queued a launch job (fields._launched_by in its payload), for fairness."""
    try:
        return str((json.loads(job.payload or "{}").get("fields") or {}).get("_launched_by") or "")
    except (ValueError, TypeError):
        return ""


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
    return (db.query(models.Job).filter(models.Job.kind == kind, models.Job.status.in_(("queued", "claimed", "running")))
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


_progress_at: dict[int, float] = {}     # job id -> monotonic time of its last commit


def progress(db: Session, job: models.Job, text: str, force: bool = False) -> None:
    """Record what a job is doing — at most one write per second per job.

    A long run calls this once per API call. Committing every one of them means a SQLite
    write per step while the page is polling for the answer, which is what makes the rest
    of the dashboard feel slow during a big run. The text is still assigned immediately,
    so the next commit (this one's or the job's own) carries the latest line."""
    text = (text or "")[:80]
    if job.progress == text:
        return
    job.progress = text
    import time as _time
    now = _time.monotonic()
    if not force and now - _progress_at.get(job.id, 0.0) < 1.0:
        return                       # kept in the session; flushed with the next commit
    _progress_at[job.id] = now
    db.commit()


def run_job(db: Session, job: models.Job) -> None:
    if job.status == CANCELLED:        # cancelled between being picked and started
        return
    if job.status == "queued" and not _claim(db, job.id):     # direct callers (tests) — same atomic take
        return
    db.refresh(job)
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
        if job.status == "error":
            # a handler that reports failure without raising — the BC runs do exactly this
            try:
                from . import diag
                diag.record("job", job.kind, "reported-failure", job.detail,
                            {"job_id": job.id, "title": job.title,
                             "payload": json.loads(job.payload or "{}")})
            except Exception:      # noqa: BLE001
                pass
        if res.get("href"):
            job.href = str(res["href"])
    except Exception as e:  # noqa: BLE001 — a job must never kill the worker
        log.exception("job %s (%s) failed", job.id, job.kind)
        db.rollback()
        job = db.merge(job)
        job.status = "error"
        job.detail = f"{type(e).__name__}: {str(e)[:400]}"
        try:
            from . import diag
            diag.record("job", job.kind, type(e).__name__, str(e)[:1500],
                        {"job_id": job.id, "title": job.title,
                         "payload": json.loads(job.payload or "{}")})
        except Exception:      # noqa: BLE001
            pass
    job.finished_at = _now()
    job.progress = ""
    _progress_at.pop(job.id, None)     # the throttle map must not grow with every job
    if job.quiet and job.status == "done":
        job.seen = True                # scheduled housekeeping that worked: no toast, just the Jobs list
    db.commit()


_running_launch_users: dict[int, str] = {}     # job id → user, for the launch lane's fairness


def _claim(db: Session, job_id: int) -> bool:
    """Atomically take a queued job (several launch workers share one lane): the UPDATE
    only succeeds for the thread that gets there first."""
    n = (db.query(models.Job).filter(models.Job.id == job_id, models.Job.status == "queued")
         .update({models.Job.status: "claimed"}, synchronize_session=False))
    db.commit()
    return bool(n)


def _next_launch(db: Session):
    """Oldest queued launch — but a user who already has a launch running yields to a
    user who doesn't (round-robin across people, oldest-first within a person)."""
    queued = (db.query(models.Job).filter(models.Job.status == "queued", models.Job.kind.in_(LAUNCH_KINDS))
              .order_by(models.Job.id).limit(50).all())
    if not queued:
        return None
    busy = set(_running_launch_users.values())
    for j in queued:
        if _launched_by(j) not in busy:
            return j
    return queued[0]


def run_pending(db: Session, limit: int = 20, which: str | None = None) -> int:
    """Run queued jobs oldest-first (used by the workers, and directly by tests).
    which = "fast" / "slow" / "launch" restricts to that lane; None runs everything."""
    n = 0
    for _ in range(limit):
        if which == "launch":
            job = _next_launch(db)
        else:
            q = db.query(models.Job).filter(models.Job.status == "queued")
            if which == "slow":
                q = q.filter(models.Job.kind.in_(SLOW_KINDS))
            elif which == "sync":
                q = q.filter(models.Job.kind.in_(SYNC_KINDS))
            elif which == "fast":
                q = q.filter(~models.Job.kind.in_(SLOW_KINDS), ~models.Job.kind.in_(LAUNCH_KINDS), ~models.Job.kind.in_(SYNC_KINDS))
            job = q.order_by(models.Job.id).first()
        if not job:
            break
        if not _claim(db, job.id):
            continue                       # another worker took it — look again
        db.refresh(job)
        if which == "launch":
            _running_launch_users[job.id] = _launched_by(job)
        try:
            run_job(db, job)
        finally:
            _running_launch_users.pop(job.id, None)
        n += 1
    return n


FINISHED = ("done", "error", CANCELLED)


def clear_finished(db: Session) -> int:
    """The one-button clear on the Jobs page: drop every finished job (done, failed,
    cancelled) from the list. Queued and running ones are never touched."""
    n = db.query(models.Job).filter(models.Job.status.in_(FINISHED)).delete(synchronize_session=False)
    db.commit()
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
        for i in range(LAUNCH_WORKERS if which == "launch" else 1):
            threading.Thread(target=_loop, args=(which,), name=f"adops-jobs-{which}-{i + 1}", daemon=True).start()


def recover(db: Session) -> int:
    """At boot: jobs still 'running' or 'queued' from a previous process. Queued
    ones will run; 'running' ones died mid-way — mark them so nobody waits."""
    n = 0
    for j in db.query(models.Job).filter(models.Job.status.in_(("running", "claimed"))).all():
        j.status = "error"
        j.detail = "interrupted by a restart — check the result page before retrying"
        j.finished_at = _now()
        n += 1
    db.commit()
    return n


def summary(db: Session) -> dict:
    running = db.query(models.Job).filter(models.Job.status.in_(("running", "claimed"))).count()
    queued = db.query(models.Job).filter(models.Job.status == "queued").count()
    return {"running": running, "queued": queued}
