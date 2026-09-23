"""Audit log — who changed what, and from where.

Two sources, one table (AuditLog):
  * every state-changing request (POST/PUT/PATCH/DELETE) by a signed-in user, written by the
    middleware after the response (method + path + status), minus pure UI chatter (autosave,
    "seen" marks, pickers' favourites) listed in QUIET;
  * named entries for the sensitive events — 2FA on/off/reset, password change, users added /
    removed / deactivated, sessions signed out, cookies and mailbox credentials saved or
    cleared, TikTok connected — recorded where they happen, with a readable detail.
Kept AUDIT_KEEP_DAYS. Nothing secret is ever written here (no bodies, no form values).
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

AUDIT_KEEP_DAYS = 180
QUIET = ("/super-launcher/autosave", "/jobs/seen", "/alerts/seen", "/inbox/seen", "/instant-pages/mark",
         "/profile-favorites", "/ui/", "/assistant/", "/notes/draft")
METHODS = ("POST", "PUT", "PATCH", "DELETE")


def should_record(method: str, path: str, authed: bool) -> bool:
    """Pure: which requests the middleware writes."""
    if not authed or method.upper() not in METHODS:
        return False
    return not any(path.startswith(q) for q in QUIET)


def record(db, models, action: str, *, user=None, ip: str = "", target: str = "", detail: str = "", status: int = 0,
           commit: bool = True) -> None:
    """Add one entry. Never raises (an audit problem must not break the action it records)."""
    try:
        db.add(models.AuditLog(at=datetime.utcnow(), user_id=getattr(user, "id", None),
                               user_email=(getattr(user, "email", "") or "")[:200], ip=(ip or "")[:64],
                               action=action[:120], target=str(target or "")[:200], status=int(status or 0),
                               detail=str(detail or "")[:1000]))
        if commit:
            from .database import safe_commit
            safe_commit(db)
    except Exception:      # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def from_request(db, models, request, action: str, target: str = "", detail: str = "") -> None:
    """record() with the signed-in user and client IP of this request."""
    from . import auth_security as sec
    user = getattr(getattr(request, "state", None), "user", None)
    if user is None:
        try:
            from .routes.auth import current_user
            user = current_user(request, db)
        except Exception:  # noqa: BLE001
            user = None
    record(db, models, action, user=user, ip=sec.client_ip(request), target=target, detail=detail)


_Q: deque = deque(maxlen=5000)          # the middleware's entries, written in batches off the request
_WAKE = threading.Event()
_started = threading.Event()


def submit(user, ip: str, method: str, path: str, status: int) -> None:
    """The middleware's entry: queued (never delays the response), written by one thread."""
    _Q.append({"at": datetime.utcnow(), "user_id": getattr(user, "id", None), "user_email": (getattr(user, "email", "") or "")[:200],
               "ip": (ip or "")[:64], "action": f"{method.upper()} {path}"[:120], "status": int(status or 0)})
    if not _started.is_set():
        _started.set()
        threading.Thread(target=_writer, name="adops-audit", daemon=True).start()
    _WAKE.set()


def flush() -> int:
    """Write what's queued (one transaction). Returns how many."""
    from .database import SessionLocal
    from . import models
    batch = []
    while _Q and len(batch) < 500:
        batch.append(_Q.popleft())
    if not batch:
        return 0
    d = SessionLocal()
    try:
        for e in batch:
            d.add(models.AuditLog(target="", detail="", **e))
        d.commit()
    except Exception:      # noqa: BLE001 — busy database: put them back for the next round
        d.rollback()
        _Q.extendleft(reversed(batch))
        return 0
    finally:
        d.close()
    return len(batch)


def _writer() -> None:
    import time as _t
    while True:
        _WAKE.wait(30)
        _WAKE.clear()
        _t.sleep(2)                                   # batch a burst of clicks into one write
        try:
            while flush():
                pass
        except Exception:  # noqa: BLE001
            pass


def recent(db, models, limit: int = 200, user_id: int | None = None, q: str = "") -> list:
    qry = db.query(models.AuditLog).order_by(models.AuditLog.at.desc())
    if user_id is not None:
        qry = qry.filter(models.AuditLog.user_id == user_id)
    if q:
        like = f"%{q}%"
        qry = qry.filter((models.AuditLog.action.like(like)) | (models.AuditLog.target.like(like))
                         | (models.AuditLog.user_email.like(like)) | (models.AuditLog.ip.like(like)))
    return qry.limit(limit).all()


def prune(db, models, days: int = AUDIT_KEEP_DAYS) -> int:
    n = db.query(models.AuditLog).filter(models.AuditLog.at < datetime.utcnow() - timedelta(days=days)).delete(synchronize_session=False)
    db.commit()
    return int(n or 0)
