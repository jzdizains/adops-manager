"""Login hardening: constant-time password check, per-IP lockout with a
persistent attempt log, TOTP two-factor (RFC 6238, authenticator apps) with
recovery codes and trusted devices, optional IP allow-list, and a session
fingerprint so changing the password logs every device out.

Everything secret stays in env vars / the data disk; nothing here is sent to
the browser except the one-time 2FA setup key the operator chooses to scan.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy.orm import Session

from . import config, models, queries

# --- lockout policy ----------------------------------------------------------
WINDOW_MIN = 15            # failures are counted over this window
LOCK_AFTER = 5             # failures from one IP within the window → locked
LOCK_MIN = 15              # …for this long (doubles at 10 failures, ×4 at 15)
FAIL_DELAY_S = 1.0         # every wrong password waits this long (blunts brute force)
TRUST_DAYS = 30            # "trust this device" skips the code for this long



def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- password ------------------------------------------------------------------
def insecure_defaults() -> bool:
    """True when the operator never set APP_PASSWORD / SESSION_SECRET."""
    return config.APP_PASSWORD == "changeme" or config.SESSION_SECRET == "dev-secret-change-me"


def password_ok(candidate: str) -> bool:
    return hmac.compare_digest((candidate or "").encode(), config.APP_PASSWORD.encode())


# --- client address ------------------------------------------------------------
def client_ip(request: Request) -> str:
    """Render (and most proxies) put the real client first in X-Forwarded-For."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "") or "unknown"


def ip_allowed(ip: str) -> bool:
    """ALLOWED_IPS env (comma-separated IPs / CIDRs) — empty = everyone."""
    rules = [r.strip() for r in (config.ALLOWED_IPS or "").split(",") if r.strip()]
    if not rules:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for r in rules:
        try:
            if "/" in r:
                if addr in ipaddress.ip_network(r, strict=False):
                    return True
            elif addr == ipaddress.ip_address(r):
                return True
        except ValueError:
            continue
    return False


# --- attempts + lockout ----------------------------------------------------------
def record_attempt(db: Session, ip: str, ok: bool, note: str = "", email: str = "") -> None:
    db.add(models.LoginAttempt(ip=ip, ok=ok, note=note[:120], email=(email or "")[:254]))
    queries.log(db, f"login {'ok' if ok else 'FAILED'} from {ip}" + (f" as {email}" if email else "") + (f" — {note}" if note else ""),
                level="info" if ok else "warn", source="auth")
    db.commit()


def _failures(db: Session, col, value) -> int:
    since = _now() - timedelta(minutes=WINDOW_MIN)
    last_ok = (db.query(models.LoginAttempt).filter(col == value, models.LoginAttempt.ok == True)  # noqa: E712
               .order_by(models.LoginAttempt.at.desc()).first())
    q = db.query(models.LoginAttempt).filter(col == value, models.LoginAttempt.ok == False,  # noqa: E712
                                             models.LoginAttempt.at >= since)
    if last_ok:
        q = q.filter(models.LoginAttempt.at > last_ok.at)
    return q.count()


def recent_failures(db: Session, ip: str) -> int:
    return _failures(db, models.LoginAttempt.ip, ip)


def _lock_seconds(db: Session, col, value, n: int) -> int:
    if n < LOCK_AFTER:
        return 0
    mult = 4 if n >= 15 else 2 if n >= 10 else 1
    last = (db.query(models.LoginAttempt).filter(col == value, models.LoginAttempt.ok == False)  # noqa: E712
            .order_by(models.LoginAttempt.at.desc()).first())
    until = last.at + timedelta(minutes=LOCK_MIN * mult)
    return max(0, int((until - _now()).total_seconds()))


def locked_for(db: Session, ip: str, email: str = "") -> int:
    """Seconds to wait: the IP is locked after LOCK_AFTER failures, and so is
    an email address (so a distributed guess at one account also stops)."""
    wait = _lock_seconds(db, models.LoginAttempt.ip, ip, recent_failures(db, ip))
    if email:
        wait = max(wait, _lock_seconds(db, models.LoginAttempt.email, email, _failures(db, models.LoginAttempt.email, email)))
    return wait


def prune_attempts(db: Session, days: int = 30) -> None:
    db.query(models.LoginAttempt).filter(models.LoginAttempt.at < _now() - timedelta(days=days)).delete()
    db.commit()


# --- TOTP (RFC 6238; SHA-1, 30 s, 6 digits — what Google Authenticator / 1Password / Authy expect) ---
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // 30))


def totp_ok(secret_b32: str, code: str, at: float | None = None) -> bool:
    """Accepts the current step and one either side (clock drift)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6 or not secret_b32:
        return False
    step = int((at if at is not None else time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret_b32, step + d), code) for d in (-1, 0, 1))


def otpauth_uri(secret_b32: str, account: str = "operator") -> str:
    from urllib.parse import quote
    issuer = quote(config.APP_NAME)
    return f"otpauth://totp/{issuer}:{quote(account)}?secret={secret_b32}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def qr_svg(text: str) -> str:
    """QR code as inline SVG when the optional `qrcode` package is installed,
    else '' (the setup key is always shown for manual entry)."""
    try:
        import qrcode
        import qrcode.image.svg as _svg
    except Exception:  # noqa: BLE001
        return ""
    try:
        img = qrcode.make(text, image_factory=_svg.SvgPathImage, box_size=6, border=2)
        return img.to_string(encoding="unicode") if hasattr(img, "to_string") else ""
    except Exception:  # noqa: BLE001
        return ""


def totp_enabled(user) -> bool:
    return bool(user is not None and user.totp_secret)


def enable_totp(db: Session, user, secret_b32: str) -> list[str]:
    """Turn 2FA on for this user; returns the recovery codes (shown once, stored hashed)."""
    codes = [f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}" for _ in range(8)]
    user.totp_secret = secret_b32
    user.totp_recovery = json.dumps([_hash_code(c) for c in codes])
    user.totp_enabled_at = _now().isoformat(timespec="seconds")
    queries.log(db, f"two-factor authentication enabled for {user.email}", source="auth")
    db.commit()
    return codes


def disable_totp(db: Session, user) -> None:
    user.totp_secret, user.totp_recovery, user.totp_enabled_at = "", "[]", ""
    queries.log(db, f"two-factor authentication DISABLED for {user.email}", level="warn", source="auth")
    db.commit()


def _hash_code(code: str) -> str:
    return hashlib.sha256(("rc:" + code.replace("-", "").replace(" ", "") + config.SESSION_SECRET).encode()).hexdigest()


def recovery_ok(db: Session, user, code: str) -> bool:
    """A recovery code works once."""
    try:
        hashes = json.loads(user.totp_recovery or "[]")
    except ValueError:
        hashes = []
    h = _hash_code(code or "")
    for stored in hashes:
        if hmac.compare_digest(stored, h):
            hashes.remove(stored)
            user.totp_recovery = json.dumps(hashes)
            queries.log(db, f"recovery code used by {user.email} — {len(hashes)} left", level="warn", source="auth")
            db.commit()
            return True
    return False


def recovery_left(user) -> int:
    try:
        return len(json.loads(user.totp_recovery or "[]"))
    except (ValueError, AttributeError):
        return 0


# --- trusted devices ("don't ask for a code on this browser for 30 days") -------
TRUST_COOKIE = "adops_trust"


def _signer() -> TimestampSigner:
    return TimestampSigner(config.SESSION_SECRET, salt="adops-trusted-device")


def _trust_value(user) -> str:
    # bound to the user AND the current 2FA secret: turning 2FA off/on re-asks everyone
    return hashlib.sha256(f"{user.id}:{user.totp_secret}".encode()).hexdigest()[:24]


def trust_token(user) -> str:
    return _signer().sign(_trust_value(user).encode()).decode()


def device_trusted(user, request: Request) -> bool:
    raw = request.cookies.get(TRUST_COOKIE, "")
    if not raw:
        return False
    try:
        val = _signer().unsign(raw.encode(), max_age=TRUST_DAYS * 86400).decode()
    except BadSignature:
        return False
    return hmac.compare_digest(val, _trust_value(user))


def recent_logins(db: Session, limit: int = 12) -> list[models.LoginAttempt]:
    return (db.query(models.LoginAttempt).order_by(models.LoginAttempt.at.desc()).limit(limit).all())
