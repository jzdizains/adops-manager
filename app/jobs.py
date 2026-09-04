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


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enqueue(db: Session, kind: str, title: str, payload: dict | None = None, href: str = "") -> models.Job:
    job = models.Job(kind=kind, title=title[:200], payload=json.dumps(payload or {}, default=str),
                     href=href or "", status="queued")
    db.add(job)
    db.commit()
    if _inline():
        run_job(db, job)          # no worker thread (tests / ADOPS_DISABLE_BG) → do it now
        return job
    _wake.set()
    return job


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
    db.commit()


def run_pending(db: Session, limit: int = 20) -> int:
    """Run queued jobs oldest-first (used by the worker, and directly by tests)."""
    n = 0
    for _ in range(limit):
        job = (db.query(models.Job).filter(models.Job.status == "queued")
               .order_by(models.Job.id).first())
        if not job:
            break
        run_job(db, job)
        n += 1
    return n


def prune(db: Session, keep_days: int = 14) -> int:
    cutoff = _now() - timedelta(days=keep_days)
    return db.query(models.Job).filter(models.Job.created_at < cutoff).delete()


def _loop():
    from .database import SessionLocal
    while True:
        _wake.wait(timeout=5.0)
        _wake.clear()
        db = SessionLocal()
        try:
            # a job left 'running' by a crashed process would block nothing, but mark it so it's visible
            run_pending(db)
        except Exception:  # noqa: BLE001
            log.exception("jobs worker sweep failed")
        finally:
            db.close()


def start() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_loop, name="adops-jobs", daemon=True)
    t.start()


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
