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
import re
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
def ip_from_chain(xff: str, peer: str, hops: int) -> str:
    """The client IP from X-Forwarded-For, counting `hops` trusted proxies from the RIGHT.
    Each proxy APPENDS the address it saw, so the entries on the left are whatever the
    caller chose to send — taking the leftmost let anyone pick their IP (and walk past the
    lockout and ALLOWED_IPS). hops=0 (no proxy in front) = the socket's peer. Pure."""
    parts = [p.strip() for p in (xff or "").split(",") if p.strip()]
    if hops <= 0 or not parts:
        return (peer or "unknown")[:64]
    return parts[-hops][:64] if len(parts) >= hops else parts[0][:64]


def client_ip(request: Request) -> str:
    """Real client address behind TRUSTED_PROXY_HOPS proxies (Render = 1)."""
    return ip_from_chain(request.headers.get("x-forwarded-for", ""),
                         request.client.host if request.client else "", config.TRUSTED_PROXY_HOPS)


UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
CSRF_EXEMPT = ("/postback", "/t/", "/pub/src/", "/oauth/callback", "/health")


def origin_ok(method: str, path: str, host: str, origin: str = "", referer: str = "",
              fetch_site: str = "", allowed: list | None = None) -> bool:
    """Cross-site request check for state-changing requests (pure). A browser always says
    where a POST comes from — Origin (every modern browser), else Referer, else at least
    Sec-Fetch-Site. It must be this site (or ALLOWED_ORIGINS). A request with none of these
    isn't from a browser page at all (curl, a server) and is left to the login check."""
    from urllib.parse import urlparse
    if method.upper() not in UNSAFE_METHODS or path.startswith(CSRF_EXEMPT):
        return True
    fs = (fetch_site or "").lower()
    if fs in ("same-origin", "none"):
        return True
    src = origin if origin and origin != "null" else referer
    host = (host or "").lower()
    if src:
        try:
            u = urlparse(src)
        except ValueError:
            return False
        net = (u.netloc or "").lower()
        if net and net == host:
            return True
        base = f"{u.scheme}://{net}".lower()
        return any(base == a or net == a for a in (allowed or []))
    return fs not in ("cross-site", "same-site")


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
def record_attempt(db: Session, ip: str, ok: bool, note: str = "", email: str = "", kind: str = "login",
                   ua: str = "", path: str = "") -> None:
    db.add(models.LoginAttempt(ip=ip, ok=ok, note=note[:120], email=(email or "")[:254], kind=kind,
                               ua=(ua or "")[:500], path=(path or "")[:200]))
    if kind != "probe":
        queries.log(db, f"login {'ok' if ok else 'FAILED'} from {ip}" + (f" as {email}" if email else "") + (f" — {note}" if note else ""),
                    level="info" if ok else "warn", source="auth")
    db.commit()


PROBE_EVERY_MIN = 10


def record_probe(db: Session, ip: str, path: str, ua: str) -> None:
    """An unauthenticated hit on a dashboard URL — 'who tried to access'.
    One row per IP per 10 minutes (the row counts repeats) so a scanner
    can't fill the table."""
    since = _now() - timedelta(minutes=PROBE_EVERY_MIN)
    recent = (db.query(models.LoginAttempt).filter(models.LoginAttempt.ip == ip, models.LoginAttempt.kind == "probe",
                                                   models.LoginAttempt.at >= since).order_by(models.LoginAttempt.at.desc()).first())
    if recent:
        m = re.match(r"(\d+) hit", recent.note or "")
        n = (int(m.group(1)) if m else 1) + 1
        recent.note = f"{n} hits in {PROBE_EVERY_MIN} min (last: {path[:60]})"
        db.commit()
        return
    record_attempt(db, ip, False, note="1 hit — not logged in", kind="probe", ua=ua, path=path)


EMAIL_LOCK_AFTER = 20      # one email across MANY IPs: a distributed guess — locked, but far later than
                           # a pair, so a stranger can't lock the owner out with five wrong passwords


def _failures(db: Session, col, value, *more) -> int:
    """Failures since the last success for this key (col == value [and more conditions])."""
    since = _now() - timedelta(minutes=WINDOW_MIN)
    last_ok = (db.query(models.LoginAttempt).filter(col == value, *more, models.LoginAttempt.ok == True)  # noqa: E712
               .order_by(models.LoginAttempt.at.desc()).first())
    q = db.query(models.LoginAttempt).filter(col == value, *more, models.LoginAttempt.ok == False,  # noqa: E712
                                             models.LoginAttempt.kind != "probe", models.LoginAttempt.at >= since)
    if last_ok:
        q = q.filter(models.LoginAttempt.at > last_ok.at)
    return q.count()


def recent_failures(db: Session, ip: str) -> int:
    return _failures(db, models.LoginAttempt.ip, ip)


def lock_minutes(n: int, after: int = LOCK_AFTER) -> int:
    """How long n failures lock for: LOCK_MIN at `after`, ×2 at double, ×4 at triple. Pure."""
    if n < after:
        return 0
    return LOCK_MIN * (4 if n >= 3 * after else 2 if n >= 2 * after else 1)


def _lock_seconds(db: Session, col, value, n: int, *more, after: int = LOCK_AFTER) -> int:
    mins = lock_minutes(n, after)
    if not mins:
        return 0
    last = (db.query(models.LoginAttempt).filter(col == value, *more, models.LoginAttempt.ok == False,  # noqa: E712
                                                 models.LoginAttempt.kind != "probe")
            .order_by(models.LoginAttempt.at.desc()).first())
    if last is None:
        return 0
    until = last.at + timedelta(minutes=mins)
    return max(0, int((until - _now()).total_seconds()))


def locked_for(db: Session, ip: str, email: str = "") -> int:
    """Seconds to wait. Keyed like the reference tool: the IP (LOCK_AFTER), the (email, IP)
    pair (LOCK_AFTER) and — much later — the email on its own (EMAIL_LOCK_AFTER), so a
    distributed guess still stops but nobody can lock someone else out from their own IP."""
    wait = _lock_seconds(db, models.LoginAttempt.ip, ip, recent_failures(db, ip))
    if email:
        E, I = models.LoginAttempt.email, models.LoginAttempt.ip
        wait = max(wait, _lock_seconds(db, E, email, _failures(db, E, email, I == ip), I == ip))
        wait = max(wait, _lock_seconds(db, E, email, _failures(db, E, email), after=EMAIL_LOCK_AFTER))
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


def totp_step(secret_b32: str, code: str, at: float | None = None) -> int | None:
    """The time step the code belongs to (current, or one either side for clock drift),
    None when it doesn't match. An unusable secret never matches (and never raises)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6 or not secret_b32:
        return None
    step = int((at if at is not None else time.time()) // 30)
    for d in (-1, 0, 1):
        try:
            if hmac.compare_digest(str(_hotp(secret_b32, step + d)).encode(), str(code).encode()):
                return step + d
        except (ValueError, TypeError):          # (binascii.Error is a ValueError) — e.g. a secret that won't decrypt
            return None
    return None


def totp_ok(secret_b32: str, code: str, at: float | None = None) -> bool:
    """Accepts the current step and one either side (clock drift)."""
    return totp_step(secret_b32, code, at) is not None


def totp_accept(db: Session, user, code: str, at: float | None = None) -> bool:
    """totp_ok + replay guard: each code works ONCE — a step at or before the last one used
    is refused (a code read over someone's shoulder, or captured, can't be replayed within
    its 90-second window)."""
    step = totp_step(user.totp_secret, code, at)
    if step is None or step <= int(getattr(user, "totp_last_step", 0) or 0):
        return False
    user.totp_last_step = step
    db.commit()
    return True


def otpauth_uri(secret_b32: str, account: str = "operator") -> str:
    from urllib.parse import quote
    issuer = quote(config.APP_NAME)
    return f"otpauth://totp/{issuer}:{quote(account)}?secret={secret_b32}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def qr_svg(text: str) -> str:
    """QR code as inline SVG (vendored python-qrcode, SVG path factory).
    '' only if generation fails — the setup key is always shown as well."""
    try:
        from .vendor import qrcode as _qr
        from .vendor.qrcode.image import svg as _svg
        img = _qr.make(text, image_factory=_svg.SvgPathImage, box_size=6, border=2)
        s = img.to_string(encoding="unicode")
        # size it for the page: a fixed 200×200 CSS box, path scales with the viewBox
        return re.sub(r'width="[^"]+" height="[^"]+"', 'width="200" height="200"', s, count=1)
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


def _hash_code(code: str, secret: str | None = None) -> str:
    return hashlib.sha256(("rc:" + code.replace("-", "").replace(" ", "") + (config.SESSION_SECRET if secret is None else secret)).encode()).hexdigest()


def _old_secrets() -> list[str]:
    """Previous SESSION_SECRETs (SECRETS_KEY_OLD) — recovery codes made before a rotation still work."""
    import os
    return [k.strip() for k in os.environ.get("SECRETS_KEY_OLD", "").split(",") if k.strip()]


def recovery_ok(db: Session, user, code: str) -> bool:
    """A recovery code works once."""
    try:
        hashes = json.loads(user.totp_recovery or "[]")
    except ValueError:
        hashes = []
    cands = [_hash_code(code or "")] + [_hash_code(code or "", s) for s in _old_secrets()]
    for stored in list(hashes):
        if any(hmac.compare_digest(stored, h) for h in cands):
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
    return hmac.compare_digest(str(val).encode(), str(_trust_value(user)).encode())


TRUST_SETTING = "allow_trusted_devices"


def trusted_devices_allowed(db: Session) -> bool:
    """Owner switch: may a browser skip the code for 30 days? Off by default —
    the code is asked at every login."""
    return queries.get_setting(db, TRUST_SETTING, "0") == "1"


def recent_logins(db: Session, limit: int = 12, kinds: tuple[str, ...] | None = None, email: str | None = None) -> list[models.LoginAttempt]:
    """`email`: only that account's attempts — a member sees their own logins, never
    the team's (the owner's Access log has everyone)."""
    q = db.query(models.LoginAttempt)
    if kinds:
        q = q.filter(models.LoginAttempt.kind.in_(kinds))
    if email is not None:
        q = q.filter(models.LoginAttempt.email == email)
    return q.order_by(models.LoginAttempt.at.desc()).limit(limit).all()


def access_log(db: Session, limit: int = 60) -> list[dict]:
    """Recent attempts + probes, enriched with device and location (a few
    fresh geo lookups per render; the rest come from the cache)."""
    from . import geo, ua as ua_mod
    budget = [geo.MAX_LOOKUPS_PER_RENDER]
    out = []
    for a in recent_logins(db, limit):
        g = geo.lookup(db, a.ip, budget)
        d = ua_mod.parse(a.ua or "")
        out.append({"a": a, "device": d, "where": geo.label(g, a.ip), "org": (g.org if g else "") or ""})
    return out


def touch_seen(db: Session, user, ip: str, ua: str) -> None:
    """Called on requests: keeps last_seen / last_ip / last_ua, at most every 5 min.

    This runs on EVERY logged-in request, so its write must never take a page
    down: if SQLite's one writer is busy (a sweep mid-write), skip the update —
    it's only 'last active', and the page renders from reads either way."""
    now = _now()
    if user.last_seen_at and (now - user.last_seen_at) < timedelta(minutes=5) and user.last_ip == ip:
        return
    user.last_seen_at, user.last_ip, user.last_ua = now, ip[:64], (ua or "")[:500]
    from .database import safe_commit
    safe_commit(db)
