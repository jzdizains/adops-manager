"""FastAPI app — registers the routers, session middleware, auth gate, static."""
import os
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import background, config
from .database import init_db
from .routes import (
    ad_texts, alerts, appeals_page, assistant_page, audience, auth, automation, campaigns, jobs_page, partners_page, cookies_admin, creatives, dashboard, display_cards,
    inbox, instant_pages, issues_page, lead_forms, locations, monitor, notes, oauth, pnl_page,
    performance, pixels, postback, security, settings_page, spark_codes, escape_test, tracking as tracking_routes,
    status, super_launcher, templates_routes,
)

app = FastAPI(title=config.APP_NAME, docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")


SECURITY_HEADERS = {
    # nothing on this app should ever be framed, sniffed, or indexed
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Robots-Tag": "noindex, nofollow",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # scripts/styles are our own (inline included); images/media may come from TikTok's CDNs
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob: https:; media-src 'self' blob: https:; font-src 'self' data:; "
                                "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'; object-src 'none'"),
}


POSTBACK_ONLY_PATHS = ("/postback", "/health", "/t/escape", "/t/click", "/t/c", "/static")


_AUTH_CACHE: dict = {}          # (uid, fp) -> (expires_at, User)  — a few seconds, cleared on logout/password change
_AUTH_TTL_S = 8


def _auth_user(request, uid, fp, ip: str, ua: str):
    """Resolve the session's user (thread pool). Cached for a few seconds so the
    4-second job poller and page assets don't each open a DB session."""
    import time as _time
    from . import auth_security as sec
    from .database import SessionLocal as _SL
    if not uid:
        return None
    key = (uid, fp)
    hit = _AUTH_CACHE.get(key)
    if hit and hit[0] > _time.time():
        return hit[1]
    d = _SL()
    try:
        user = auth.current_user(request, d)
        if user is not None:
            try:
                sec.touch_seen(d, user, ip, ua)
            except Exception:  # noqa: BLE001
                d.rollback()
            _AUTH_CACHE[key] = (_time.time() + _AUTH_TTL_S, user)
        else:
            _AUTH_CACHE.pop(key, None)
        if len(_AUTH_CACHE) > 500:
            _AUTH_CACHE.clear()
        return user
    finally:
        d.close()


def auth_cache_clear():
    _AUTH_CACHE.clear()


# ANY change to a user row (password, 2FA set up, deactivated, role, sign-out-everywhere,
# deleted) empties the cache — no call site has to remember to.
from sqlalchemy import event as _sa_event  # noqa: E402
from . import models as _models  # noqa: E402


@_sa_event.listens_for(_models.User, "after_update")
@_sa_event.listens_for(_models.User, "after_insert")
@_sa_event.listens_for(_models.User, "after_delete")
def _user_changed(_mapper, _conn, _target):
    _AUTH_CACHE.clear()


def _record_probe(ip: str, path: str, ua: str) -> None:
    from . import auth_security as sec
    from .database import SessionLocal as _SL
    d = _SL()
    try:
        sec.record_probe(d, ip, path, ua)
    except Exception:  # noqa: BLE001
        d.rollback()
    finally:
        d.close()


def _not_found():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("Not found", status_code=404)


@app.middleware("http")
async def require_login(request: Request, call_next):
    from . import auth_security as sec
    host = (request.headers.get("host") or "").split(":")[0].lower()
    path = request.url.path
    # a dedicated postback hostname never serves the dashboard (nor its login page)
    if config.POSTBACK_HOST and host == config.POSTBACK_HOST and not path.startswith(POSTBACK_ONLY_PATHS):
        return _not_found()
    hidden = config.LOGIN_PATH != "/login"
    if hidden:
        # the real login lives at the secret path; the well-known one is a 404
        if path == config.LOGIN_PATH or path.startswith(config.LOGIN_PATH + "/"):
            request.scope["path"] = "/login" + path[len(config.LOGIN_PATH):]
            request.scope["raw_path"] = request.scope["path"].encode()
            path = request.scope["path"]
        elif path == "/login" or path.startswith("/login/"):
            return _not_found()
    public = path.startswith(auth.PUBLIC_PATHS)
    if not public:
        import time as _time
        sess = request.session
        if sess.get("authed"):
            # a changed password, a deactivated user, "sign out everywhere" or an
            # expired session logs this device out. The lookup runs OFF the event loop
            # (a locked SQLite file must never stall every other user's request) and
            # is remembered for a few seconds per session so pollers don't hit the DB.
            user = await run_in_threadpool(_auth_user, request, sess.get("uid"), sess.get("fp"), sec.client_ip(request), request.headers.get("user-agent", ""))
            if user is None or _time.time() - float(sess.get("at") or 0) > config.SESSION_MAX_AGE_S:
                sess.clear()
                return RedirectResponse(f"{config.LOGIN_PATH}?err=expired", status_code=303)
            request.state.user = user
            # 2FA is mandatory: until it's set up, only the setup page (and logout) is reachable
            if config.REQUIRE_2FA and not user.totp_secret and not (path.startswith("/login/2fa/setup") or path == "/logout"):
                return RedirectResponse(f"{config.LOGIN_PATH}/2fa/setup", status_code=303)
        else:
            # someone who isn't logged in asked for a dashboard URL — remember who
            if not path.startswith(("/creatives/", "/static")) or path.count("/") < 3:
                await run_in_threadpool(_record_probe, sec.client_ip(request), path, request.headers.get("user-agent", ""))
            if hidden:
                return _not_found()          # don't even hint that there is something to log in to
            return RedirectResponse(f"/login?next={path}", status_code=303)
    try:
        resp = await call_next(request)
    except Exception as exc:  # noqa: BLE001 — turn a bare "Internal Server Error" into something actionable
        resp = _error_page(request, exc)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not path.startswith("/static") and "text/html" in (resp.headers.get("content-type") or ""):
        # logged-in pages must never come back from the browser cache after logout / on a shared machine
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


def _error_page(request: Request, exc: BaseException):
    """A readable error page (logged-in users see the failing line and the
    traceback tail so they can paste it to whoever fixes it) + an AppLog row.
    Render's default gives nothing but the words 'Internal Server Error'."""
    import traceback as _tb

    from fastapi.responses import HTMLResponse
    from .database import SessionLocal
    from . import queries
    from .templating import render
    frames = _tb.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "/app/" in (f.filename or "").replace("\\", "/")] or frames
    where = ours[-1] if ours else None
    tail = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)[-12:])
    summary = f"{type(exc).__name__}: {str(exc)[:300]}"
    try:
        d = SessionLocal()
        try:
            queries.log(d, f"{request.method} {request.url.path}: {summary}"
                        + (f" @ {where.filename.rsplit('/', 1)[-1]}:{where.lineno} {where.name}()" if where else ""),
                        level="error", source="http")
            d.commit()
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        pass
    authed = bool(request.session.get("authed")) if "session" in request.scope else False
    try:
        resp = render(request, "error.html", {
            "title": "Something broke", "path": request.url.path, "summary": summary if authed else "",
            "where": (f"{where.filename.rsplit('/', 1)[-1]} line {where.lineno} in {where.name}()" if (where and authed) else ""),
            "tail": tail if authed else "", "active": "",
        })
        resp.status_code = 500
        return resp
    except Exception:  # noqa: BLE001 — even the error page failed: plain text, still with the summary
        return HTMLResponse(f"<pre>Something broke on {request.url.path}\n{summary if authed else ''}</pre>", status_code=500)


# Added AFTER the login middleware so SessionMiddleware sits OUTERMOST and has
# populated request.session before the login check runs (Starlette ordering).
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET,
                   session_cookie="adops_session", max_age=config.SESSION_MAX_AGE_S,
                   same_site="lax", https_only=not config.TEST_MODE)   # cookie: HttpOnly (always), Secure on https


# Create tables / run light migrations at import time — robust under uvicorn,
# TestClient, and one-off scripts alike.
init_db()
# first start: turn APP_PASSWORD into the owner account (see users.bootstrap)
try:
    from . import users as _users
    from .database import SessionLocal as _BootSL
    _d = _BootSL()
    try:
        _users.bootstrap(_d)
        _users.ensure_owner(_d)      # OWNER_EMAIL set later / renamed / locked out → still an owner you can log in as
    finally:
        _d.close()
except Exception:  # noqa: BLE001 — never keep the app from starting
    pass

# Background worker: balance sync + low-balance alerts + metric refresh.
background.start()

# Jobs worker: launches, bid changes, appeals, syncs — anything a page shouldn't wait for.
from . import job_handlers  # noqa: E402,F401 — registers the handlers
from . import jobs as _jobs  # noqa: E402
if os.environ.get("ADOPS_DISABLE_BG") != "1":
    from .database import SessionLocal as _SL
    _db = _SL()
    try:
        _jobs.recover(_db)
    finally:
        _db.close()
    _jobs.start()


for r in (auth.router, security.router, oauth.router, dashboard.router,
          templates_routes.router, campaigns.router, super_launcher.router,
          status.router, performance.router, spark_codes.router,
          instant_pages.router, lead_forms.router, cookies_admin.router,
          monitor.router, alerts.router, inbox.router,
          settings_page.router, postback.router, pixels.router,
          automation.router, issues_page.router, creatives.router,
          ad_texts.router, locations.router, escape_test.router, tracking_routes.router,
          appeals_page.router, partners_page.router, jobs_page.router, audience.router, display_cards.router, notes.router, pnl_page.router, assistant_page.router):
    app.include_router(r)
