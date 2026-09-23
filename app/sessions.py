"""Server-side sessions: every signed-in browser is a row that can be signed out on its own.

The session cookie used to carry the user id and a fingerprint; the only way to end a stolen
session was "sign out everywhere". Now login mints a random session id (UserSession); the
cookie carries it; every request checks the row still stands. Settings › Security lists the
user's devices with a "Sign out" per device.

Sessions from before v148 (a cookie with no id) are adopted on their next request — no one is
logged out by the upgrade.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

TOUCH_EVERY = timedelta(minutes=1)
KEEP_DAYS = 30


def _now():
    return datetime.utcnow()


def new_sid() -> str:
    return secrets.token_urlsafe(32)


def create(db, models, user, ip: str = "", ua: str = "") -> str:
    sid = new_sid()
    db.add(models.UserSession(sid=sid, user_id=user.id, ip=(ip or "")[:64], ua=(ua or "")[:400],
                              created_at=_now(), last_seen_at=_now()))
    db.commit()
    return sid


def check(db, models, sid: str, user_id: int, ip: str = "", max_age_s: int | None = None):
    """The live row for this cookie, or None (unknown / someone else's / revoked / too old).
    Stamps last-seen at most once a minute."""
    if not sid:
        return None
    row = db.query(models.UserSession).filter(models.UserSession.sid == sid).first()
    if row is None or row.user_id != user_id or row.revoked_at is not None:
        return None
    if max_age_s and row.created_at and (_now() - row.created_at).total_seconds() > max_age_s:
        return None
    if not row.last_seen_at or _now() - row.last_seen_at > TOUCH_EVERY:
        row.last_seen_at = _now()
        if ip:
            row.ip = ip[:64]
        try:
            db.commit()
        except Exception:      # noqa: BLE001 — a busy database never logs anyone out
            db.rollback()
    return row


def revoke(db, models, sid: str, by: str = "") -> bool:
    row = db.query(models.UserSession).filter(models.UserSession.sid == sid, models.UserSession.revoked_at.is_(None)).first()
    if row is None:
        return False
    row.revoked_at, row.revoked_by = _now(), by[:80]
    db.commit()
    return True


def revoke_user(db, models, user_id: int, except_sid: str = "", by: str = "") -> int:
    n = 0
    for row in db.query(models.UserSession).filter(models.UserSession.user_id == user_id,
                                                   models.UserSession.revoked_at.is_(None)):
        if except_sid and row.sid == except_sid:
            continue
        row.revoked_at, row.revoked_by = _now(), by[:80]
        n += 1
    db.commit()
    return n


def active_for(db, models, user_id: int, max_age_s: int | None = None) -> list:
    cut = _now() - timedelta(seconds=max_age_s) if max_age_s else None
    q = (db.query(models.UserSession).filter(models.UserSession.user_id == user_id, models.UserSession.revoked_at.is_(None))
         .order_by(models.UserSession.last_seen_at.desc()))
    return [r for r in q.limit(50) if cut is None or (r.created_at and r.created_at >= cut)]


def prune(db, models, days: int = KEEP_DAYS) -> int:
    cut = _now() - timedelta(days=days)
    n = (db.query(models.UserSession)
         .filter((models.UserSession.last_seen_at < cut) | (models.UserSession.revoked_at < cut))
         .delete(synchronize_session=False))
    db.commit()
    return int(n or 0)


def device_label(ua: str) -> str:
    """'Chrome on macOS' from a User-Agent. Pure."""
    u = (ua or "").lower()
    browser = ("Edge" if "edg/" in u else "Opera" if "opr/" in u else "Chrome" if "chrome/" in u or "crios" in u
               else "Firefox" if "firefox/" in u or "fxios" in u else "Safari" if "safari/" in u else "Browser")
    os_ = ("iPhone" if "iphone" in u else "iPad" if "ipad" in u else "Android" if "android" in u
           else "Windows" if "windows" in u else "macOS" if "mac os" in u or "macintosh" in u else "Linux" if "linux" in u else "")
    return browser + (f" on {os_}" if os_ else "")
