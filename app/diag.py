"""The error feed — what actually went wrong, in TikTok's own words.

Every non-zero answer TikTok gives, every job that fails and every unhandled request
error lands here with the endpoint that produced it, the code, the verbatim message and
the request that provoked it. The dashboard shows it on /diagnostics.

Why it exists: an error that only appears as a red toast, or only in a screenshot, costs
a round trip to diagnose and is gone by the time anyone looks. TikTok's own wording is
usually the whole answer — "share_type: value is not one of the allowed values ...
correct is SHARED, SHARING" names the bug outright — so it is kept, not paraphrased.

Three rules this module lives by:
  * It can never break what it is watching. Every path is wrapped; a recorder that
    raises would turn a handled TikTok error into a crash.
  * It can never grow without bound. Repeats of one error count on a single row, and
    the table is pruned to KEEP rows.
  * It can never store a secret. Anything that looks like a token, key, secret or
    password is dropped before the context is written.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

log = logging.getLogger("adops.diag")

KEEP = 400                    # rows kept in the table
MERGE_WINDOW = timedelta(hours=12)    # the same error inside this window counts up, not out
RECENT = deque(maxlen=200)    # last errors in memory — survives a DB that won't take writes
_lock = Lock()

_SECRET_HINTS = ("token", "secret", "key", "password", "passwd", "auth", "signature", "cookie")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def redact(value: Any, depth: int = 0) -> Any:
    """Strip anything that could be a credential, at any depth."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:40]:
            if any(h in str(k).lower() for h in _SECRET_HINTS):
                out[str(k)] = "[redacted]"
            else:
                out[str(k)] = redact(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        return value[:400]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:400]


def record(kind: str, where: str, code: Any = "", message: str = "",
           context: Any = None, request_id: str = "") -> None:
    """Note one error. Never raises, never blocks on anything that matters."""
    try:
        row = {"kind": str(kind)[:20], "where": str(where)[:200], "code": str(code)[:40],
               "message": (message or "")[:2000], "request_id": str(request_id)[:80],
               "context": redact(context if context is not None else {}),
               "at": _utcnow().isoformat(timespec="seconds")}
        with _lock:
            RECENT.append(row)
        _store(row)
    except Exception:      # noqa: BLE001 — the watcher must never break the watched
        log.debug("diag.record failed", exc_info=True)


def _store(row: dict) -> None:
    from .database import SessionLocal
    from . import models
    db = None
    try:
        db = SessionLocal()
        now = _utcnow()
        hit = (db.query(models.DiagEvent)
               .filter(models.DiagEvent.kind == row["kind"],
                       models.DiagEvent.where == row["where"],
                       models.DiagEvent.code == row["code"],
                       models.DiagEvent.last_at >= now - MERGE_WINDOW)
               .order_by(models.DiagEvent.id.desc()).first())
        if hit is not None and (hit.message or "")[:300] == row["message"][:300]:
            hit.n = int(hit.n or 1) + 1
            hit.last_at = now
            hit.seen = False
            hit.context = json.dumps(row["context"])[:4000]
            if row["request_id"]:
                hit.request_id = row["request_id"]
        else:
            db.add(models.DiagEvent(
                kind=row["kind"], where=row["where"], code=row["code"], message=row["message"],
                context=json.dumps(row["context"])[:4000], request_id=row["request_id"],
                n=1, first_at=now, last_at=now, seen=False))
        db.commit()
        _prune(db)
    except Exception:      # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:      # noqa: BLE001
                pass
        log.debug("diag store failed", exc_info=True)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:      # noqa: BLE001
                pass


_prune_at = [datetime.min]


def _prune(db) -> None:
    """Trim to KEEP rows — at most once a minute, so a burst of errors doesn't turn into
    a burst of deletes on a 512 MB box."""
    now = _utcnow()
    if now - _prune_at[0] < timedelta(minutes=1):
        return
    _prune_at[0] = now
    from . import models
    total = db.query(models.DiagEvent).count()
    if total <= KEEP:
        return
    cutoff = (db.query(models.DiagEvent.id)
              .order_by(models.DiagEvent.id.desc()).offset(KEEP).limit(1).scalar())
    if cutoff:
        db.query(models.DiagEvent).filter(models.DiagEvent.id <= cutoff).delete(
            synchronize_session=False)
        db.commit()


def recent(db, limit: int = 100, kind: str = "", q: str = "") -> list:
    from . import models
    query = db.query(models.DiagEvent)
    if kind:
        query = query.filter(models.DiagEvent.kind == kind)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(models.DiagEvent.message.ilike(like)
                             | models.DiagEvent.where.ilike(like)
                             | models.DiagEvent.code.ilike(like))
    return query.order_by(models.DiagEvent.last_at.desc()).limit(max(1, min(limit, 400))).all()


def unseen_count(db) -> int:
    from . import models
    try:
        return db.query(models.DiagEvent).filter(models.DiagEvent.seen == False).count()  # noqa: E712
    except Exception:      # noqa: BLE001
        return 0


def mark_seen(db) -> None:
    from . import models
    db.query(models.DiagEvent).filter(models.DiagEvent.seen == False).update(  # noqa: E712
        {"seen": True}, synchronize_session=False)
    db.commit()


def as_json(row) -> dict:
    try:
        ctx = json.loads(row.context or "{}")
    except ValueError:
        ctx = {}
    return {"kind": row.kind, "where": row.where, "code": row.code, "message": row.message,
            "context": ctx, "request_id": row.request_id, "n": int(row.n or 1),
            "first_at": row.first_at.isoformat(timespec="seconds") if row.first_at else "",
            "last_at": row.last_at.isoformat(timespec="seconds") if row.last_at else ""}
