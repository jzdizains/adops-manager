"""User accounts: email + password (PBKDF2-SHA256), admin flag, per-user 2FA.

Bootstrap: the first start with an empty users table turns the env
APP_PASSWORD into the owner account (OWNER_EMAIL, admin). From then on all
logins go through the users table — only emails an admin has created can log
in at all, which is the "whitelist". A user's password change or "sign out
everywhere" bumps session_version, which invalidates that user's sessions.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import config, models, queries

MIN_PASSWORD = 12
PBKDF2_ITERS = 1_000 if config.TEST_MODE else 600_000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    e = norm_email(email)
    return 3 <= len(e) <= 254 and "@" in e and "." in e.rsplit("@", 1)[-1] and " " not in e


def hash_password(password: str, iters: int | None = None) -> str:
    iters = iters or PBKDF2_ITERS
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str:
    """'' when acceptable, else why not."""
    if len(password or "") < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if password.lower() in ("changeme", "password", "123456789012", "qwertyuiop12"):
        return "Pick a less obvious password."
    return ""


def fingerprint(user: models.User) -> str:
    """Goes into the session; changes when the password or session_version does."""
    return hashlib.sha256(f"{user.id}:{user.password_hash}:{user.session_version}:{config.SESSION_SECRET}".encode()).hexdigest()[:20]


def is_owner(user) -> bool:
    """The one account allowed to manage users: the OWNER_EMAIL env var."""
    return bool(user is not None and user.active and norm_email(user.email) == norm_email(config.OWNER_EMAIL))


def by_email(db: Session, email: str) -> models.User | None:
    e = norm_email(email)
    return db.query(models.User).filter_by(email=e).first() if e else None


def create(db: Session, email: str, password: str, is_admin: bool = False, must_change: bool = False) -> models.User:
    if not valid_email(email):
        raise ValueError("That doesn't look like an email address.")
    if by_email(db, email):
        raise ValueError("A user with that email already exists.")
    why = password_problem(password)
    if why:
        raise ValueError(why)
    u = models.User(email=norm_email(email), password_hash=hash_password(password), is_admin=is_admin,
                    active=True, must_change_password=must_change)
    db.add(u)
    db.commit()
    queries.log(db, f"user created: {u.email}" + (" (admin)" if is_admin else ""), source="auth")
    return u


def set_password(db: Session, user: models.User, password: str, by_admin: bool = False) -> None:
    why = password_problem(password)
    if why:
        raise ValueError(why)
    user.password_hash = hash_password(password)
    user.session_version = (user.session_version or 0) + 1      # every session of this user ends
    user.must_change_password = bool(by_admin)
    db.commit()
    queries.log(db, f"password {'reset by an admin' if by_admin else 'changed'} for {user.email}", source="auth")


def sign_out_everywhere(db: Session, user: models.User) -> None:
    user.session_version = (user.session_version or 0) + 1
    db.commit()


def authenticate(db: Session, email: str, password: str) -> models.User | None:
    """The user when email + password match an ACTIVE account, else None.
    Always runs the hash so timing doesn't reveal whether the email exists."""
    u = by_email(db, email)
    stored = u.password_hash if u else hash_password("x", iters=PBKDF2_ITERS)
    ok = verify_password(password, stored)
    if u and ok and u.active:
        return u
    return None


DEFAULT_OWNER = "owner@adops.local"


def ensure_owner(db: Session) -> str:
    """Every start: make sure the OWNER_EMAIL account exists and is usable.
    - exists → (optionally) reset its password to APP_PASSWORD when the
      RESET_OWNER_PASSWORD env var is "1" (the lockout escape hatch), and
      make sure it's active;
    - only the placeholder owner (owner@adops.local, minted before
      OWNER_EMAIL was set) exists → rename it, keeping password + 2FA;
    - no owner at all → create it from APP_PASSWORD.
    Returns a short description of what happened ('' = nothing)."""
    want = norm_email(config.OWNER_EMAIL) or DEFAULT_OWNER
    u = by_email(db, want)
    if u:
        changed = []
        if not u.active:
            u.active = True
            changed.append("reactivated")
        if config.RESET_OWNER_PASSWORD and config.APP_PASSWORD != "changeme":
            u.password_hash = hash_password(config.APP_PASSWORD)
            u.session_version = (u.session_version or 0) + 1
            u.must_change_password = False
            changed.append("password reset to APP_PASSWORD (remove RESET_OWNER_PASSWORD now)")
        if changed:
            db.commit()
            queries.log(db, f"owner {u.email}: " + ", ".join(changed), level="warn", source="auth")
        return ", ".join(changed)
    placeholder = by_email(db, DEFAULT_OWNER) if want != DEFAULT_OWNER else None
    if placeholder:
        old = placeholder.email
        placeholder.email = want
        placeholder.active = True
        db.commit()
        queries.log(db, f"owner account renamed {old} → {want} (OWNER_EMAIL set after first start)", source="auth")
        return f"renamed {old} → {want}"
    if config.APP_PASSWORD == "changeme" and not config.ALLOW_INSECURE_DEFAULTS:
        return ""
    u = models.User(email=want, password_hash=hash_password(config.APP_PASSWORD), is_admin=True, active=True)
    db.add(u)
    db.commit()
    queries.log(db, f"owner account created from APP_PASSWORD: {want}", source="auth")
    return f"created {want}"


def bootstrap(db: Session) -> models.User | None:
    """First run: no users → the env APP_PASSWORD becomes the owner account
    (OWNER_EMAIL, admin). A global 2FA secret from the pre-accounts build
    moves onto that owner. Returns the created owner or None."""
    if db.query(models.User).count():
        return None
    if config.APP_PASSWORD == "changeme" and not config.ALLOW_INSECURE_DEFAULTS:
        return None            # refuse to mint an owner with the placeholder password
    u = models.User(email=norm_email(config.OWNER_EMAIL) or "owner@adops.local",
                    password_hash=hash_password(config.APP_PASSWORD), is_admin=True, active=True)
    old_secret = queries.get_setting(db, "totp_secret", "")
    if old_secret:
        u.totp_secret = old_secret
        u.totp_recovery = queries.get_setting(db, "totp_recovery", "[]")
        u.totp_enabled_at = queries.get_setting(db, "totp_enabled_at", "")
        queries.set_setting(db, "totp_secret", "")
    db.add(u)
    db.commit()
    queries.log(db, f"owner account created from APP_PASSWORD: {u.email}", source="auth")
    return u


def touch_login(db: Session, user: models.User) -> None:
    user.last_login_at = _now()
    db.commit()
