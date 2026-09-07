"""App login — user accounts (email + password) with optional per-user TOTP.

Flow: POST /login (email + password; constant-time hash check; per-IP and
per-email lockout) → if the user has 2FA on and this browser isn't trusted:
/login/2fa (authenticator code or a one-time recovery code) → session authed
for that user. The session carries the user's fingerprint, so a password
change or "sign out everywhere" ends every session of that user.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import auth_security as sec
from .. import config, models, users
from ..database import SessionLocal
from ..templating import render

router = APIRouter()

# /postback is public so Glitchy's servers can reach it (auth = its key param)
PUBLIC_PATHS = ("/login", "/static", "/health", "/oauth/callback", "/favicon.ico",
                "/postback", "/t/escape")   # /t/escape/* = phone-side test pages (results stay behind login)

GENERIC = "Couldn't log in."     # never say which part was wrong
LP = config.LOGIN_PATH           # where the login page actually lives (may be a secret path)


def _db() -> Session:
    return SessionLocal()


def current_user(request: Request, db: Session) -> models.User | None:
    """The logged-in user for this request (None when the session is stale)."""
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.get(models.User, int(uid))
    if not u or not u.active or request.session.get("fp") != users.fingerprint(u):
        return None
    return u


def _login_ctx(request: Request, error: str = "", **extra) -> dict:
    return {"title": "Log in", "error": error, "login_path": LP,
            "insecure": sec.insecure_defaults() and not config.ALLOW_INSECURE_DEFAULTS,
            "email": request.query_params.get("email", "")[:254], **extra}


@router.get("/login")
def login_page(request: Request):
    if request.session.get("authed"):
        return RedirectResponse("/", status_code=303)
    err = request.query_params.get("err", "")
    msg = {"1": GENERIC, "locked": "Too many attempts — try again later.", "ip": "This network isn't allowed to log in.",
           "expired": "Your session ended — log in again.", "nouser": "No user accounts exist yet — set APP_PASSWORD (and OWNER_EMAIL) on the server and restart."}.get(err, "")
    return render(request, "login.html", _login_ctx(request, msg))


@router.post("/login")
def login_submit(request: Request, email: str = Form(""), password: str = Form("")):
    ip = sec.client_ip(request)
    ua = request.headers.get("user-agent", "")
    email = users.norm_email(email)
    db = _db()
    try:
        if sec.insecure_defaults() and not config.ALLOW_INSECURE_DEFAULTS:
            sec.record_attempt(db, ip, False, "refused: APP_PASSWORD / SESSION_SECRET not set", email, ua=ua)
            return render(request, "login.html", _login_ctx(request, ""))
        if not sec.ip_allowed(ip):
            sec.record_attempt(db, ip, False, "ip not in ALLOWED_IPS", email, ua=ua)
            return RedirectResponse(f"{LP}?err=ip", status_code=303)
        if not db.query(models.User).count():
            users.bootstrap(db)
            if not db.query(models.User).count():
                return RedirectResponse(f"{LP}?err=nouser", status_code=303)
        if not email and config.TEST_MODE:
            email = config.OWNER_EMAIL.lower()          # local tests post only the password
        wait = sec.locked_for(db, ip, email)
        if wait:
            sec.record_attempt(db, ip, False, f"locked ({wait}s left)", email, ua=ua)
            return RedirectResponse(f"{LP}?err=locked", status_code=303)
        user = users.authenticate(db, email, password)
        if not user:
            sec.record_attempt(db, ip, False, "wrong email or password", email, ua=ua)
            time.sleep(sec.FAIL_DELAY_S if not config.TEST_MODE else 0)
            return RedirectResponse(f"{LP}?err=" + ("locked" if sec.locked_for(db, ip, email) else "1"), status_code=303)
        request.session.clear()
        if sec.totp_enabled(user) and not sec.device_trusted(user, request):
            request.session["pre_2fa"] = user.id
            request.session["pre_2fa_at"] = time.time()
            return RedirectResponse(f"{LP}/2fa", status_code=303)
        _finish_login(request, db, user, ip, "password" + (" + trusted device" if sec.totp_enabled(user) else ""))
        return RedirectResponse("/settings#security" if user.must_change_password else "/", status_code=303)
    finally:
        db.close()


def _finish_login(request: Request, db: Session, user: models.User, ip: str, how: str) -> None:
    request.session.clear()
    request.session["authed"] = True
    request.session["uid"] = user.id
    request.session["fp"] = users.fingerprint(user)
    request.session["at"] = time.time()
    users.touch_login(db, user)
    sec.record_attempt(db, ip, True, how, user.email, ua=request.headers.get("user-agent", ""))
    sec.touch_seen(db, user, ip, request.headers.get("user-agent", ""))


def _pending_user(request: Request, db: Session) -> models.User | None:
    uid = request.session.get("pre_2fa")
    if not uid or time.time() - float(request.session.get("pre_2fa_at") or 0) > 300:
        return None
    u = db.get(models.User, int(uid))
    return u if (u and u.active) else None


@router.get("/login/2fa")
def twofa_page(request: Request):
    if request.session.get("authed"):
        return RedirectResponse("/", status_code=303)
    db = _db()
    try:
        if not _pending_user(request, db):
            request.session.clear()
            return RedirectResponse(f"{LP}?err=expired", status_code=303)
    finally:
        db.close()
    return render(request, "login_2fa.html", {"title": "Verification code", "error": request.query_params.get("err", ""), "login_path": LP})


@router.post("/login/2fa")
def twofa_submit(request: Request, code: str = Form(""), trust: str = Form("")):
    ip = sec.client_ip(request)
    db = _db()
    try:
        user = _pending_user(request, db)
        if not user:
            request.session.clear()
            return RedirectResponse(f"{LP}?err=expired", status_code=303)
        if sec.locked_for(db, ip, user.email):
            request.session.clear()
            return RedirectResponse(f"{LP}?err=locked", status_code=303)
        code = (code or "").strip()
        ok_code = sec.totp_ok(user.totp_secret, code)
        ok_recovery = (not ok_code) and len(code.replace("-", "").replace(" ", "")) == 8 and sec.recovery_ok(db, user, code)
        if not (ok_code or ok_recovery):
            sec.record_attempt(db, ip, False, "wrong 2fa code", user.email, kind="2fa", ua=request.headers.get("user-agent", ""))
            time.sleep(sec.FAIL_DELAY_S if not config.TEST_MODE else 0)
            if sec.locked_for(db, ip, user.email):
                request.session.clear()
                return RedirectResponse(f"{LP}?err=locked", status_code=303)
            return RedirectResponse(f"{LP}/2fa?err=1", status_code=303)
        _finish_login(request, db, user, ip, "password + " + ("recovery code" if ok_recovery else "authenticator"))
        resp = RedirectResponse("/settings#security" if user.must_change_password else "/", status_code=303)
        if trust == "1" and ok_code:
            resp.set_cookie(sec.TRUST_COOKIE, sec.trust_token(user), max_age=sec.TRUST_DAYS * 86400,
                            httponly=True, samesite="lax", secure=not config.TEST_MODE)
        return resp
    finally:
        db.close()


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(LP, status_code=303)


@router.get("/health")
def health():
    return {"ok": True}
