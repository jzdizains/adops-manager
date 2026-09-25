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
    ad_texts, alerts, appeals_page, assistant_page, audience, auth, automation, bc_assets_page, campaigns, jobs_page, partners_page, cookies_admin, creatives, creators, dashboard, diagnostics, display_cards, pub,
    team, inbox, instant_pages, issues_page, lead_forms, locations, monitor, notes, oauth, pnl_page,
    performance, pixels, postback, security, settings_page, spark_codes, escape_test, tracking as tracking_routes,
    status, super_launcher, templates_routes, landers as landers_page, warmup_page, lab_page, asset_builds_page)

# log.info/.warning from our modules used to go nowhere (no handler configured): one line each
# on stdout, which Render keeps. LOG_LEVEL=DEBUG for more.
import logging as _logging  # noqa: E402
if not _logging.getLogger().handlers:
    _logging.basicConfig(level=getattr(_logging, os.environ.get("LOG_LEVEL", "INFO").upper(), _logging.INFO),
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO — the Telegram bot API carries its token IN the URL, so
# request lines never reach the logs (v151 audit)
for _quiet in ("httpx", "httpcore"):
    _logging.getLogger(_quiet).setLevel(_logging.WARNING)

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


POSTBACK_ONLY_PATHS = ("/postback", "/health", "/t/escape", "/t/click", "/t/lp", "/t/c", "/pub/src/", "/static")


_AUTH_CACHE: dict = {}          # (uid, fp) -> (expires_at, User)  — a few seconds, cleared on logout/password change
_AUTH_TTL_S = 8


def _auth_user(request, uid, fp, ip: str, ua: str):
    """Resolve the session's user (thread pool). Cached for a few seconds so the
    4-second job poller and page assets don't each open a DB session.
    v148: the cookie's session id must name a live UserSession of this user (a device signed
    out on its own ends here); a pre-v148 cookie without one is adopted, not logged out."""
    import time as _time
    from . import auth_security as sec, sessions as _sessions
    from .database import SessionLocal as _SL
    if not uid:
        return None
    sid = request.session.get("sid") or ""
    key = (uid, fp, sid)
    hit = _AUTH_CACHE.get(key)
    if hit and hit[0] > _time.time():
        return hit[1]
    d = _SL()
    try:
        user = auth.current_user(request, d)
        if user is not None:
            if not sid:
                request.session["sid"] = _sessions.create(d, _models, user, ip, ua)
            elif _sessions.check(d, _models, sid, user.id, ip, config.SESSION_MAX_AGE_S) is None:
                user = None
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


_SEEN_ONLY = {"last_seen_at", "last_ip", "last_ua"}      # "last active" bookkeeping — not who may sign in


def _only_seen(target) -> bool:
    from sqlalchemy import inspect as _inspect
    try:
        changed = {a.key for a in _inspect(target).attrs if a.history.has_changes()}
    except Exception:      # noqa: BLE001
        return False
    return bool(changed) and changed <= _SEEN_ONLY


@_sa_event.listens_for(_models.User, "after_insert")
@_sa_event.listens_for(_models.User, "after_delete")
@_sa_event.listens_for(_models.UserSession, "after_delete")
def _user_changed(_mapper, _conn, _target):
    _AUTH_CACHE.clear()


@_sa_event.listens_for(_models.User, "after_update")
@_sa_event.listens_for(_models.UserSession, "after_update")
def _user_updated(_mapper, _conn, target):
    # the per-minute "last seen" stamps used to empty the whole cache for everyone (v151 audit)
    if not _only_seen(target):
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
    # a state-changing request from another site's page is refused before anything else
    # (login CSRF included); machine endpoints (postback, trackers, OAuth) are exempt
    if not sec.origin_ok(request.method, path, request.headers.get("host", ""), request.headers.get("origin", ""),
                         request.headers.get("referer", ""), request.headers.get("sec-fetch-site", ""), config.ALLOWED_ORIGINS):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Blocked: this request came from another site.", status_code=403)
    if not public:
        import time as _time
        sess = request.session
        if sess.get("authed"):
            # a changed password, a deactivated user, "sign out everywhere" or an
            # expired session logs this device out. The lookup runs OFF the event loop
            # (a locked SQLite file must never stall every other user's request) and
            # is remembered for a few seconds per session so pollers don't hit the DB.
            # resolving the user is a READ (WAL lets it run while a sweep writes); on the
            # rare moment the file is briefly exclusive (a checkpoint), retry once, then
            # serve a short "busy, retry" instead of a bare 500 — never log the user out
            # over a transient lock.
            from sqlalchemy.exc import OperationalError as _OpErr
            try:
                user = await run_in_threadpool(_auth_user, request, sess.get("uid"), sess.get("fp"), sec.client_ip(request), request.headers.get("user-agent", ""))
            except _OpErr:
                try:
                    await run_in_threadpool(_time.sleep, 0.2)
                    user = await run_in_threadpool(_auth_user, request, sess.get("uid"), sess.get("fp"), sec.client_ip(request), request.headers.get("user-agent", ""))
                except _OpErr:
                    from fastapi.responses import PlainTextResponse
                    return PlainTextResponse("The dashboard is busy for a moment — refreshing…", status_code=503,
                                             headers={"Retry-After": "2", "Refresh": "2"})
            if user is None or _time.time() - float(sess.get("at") or 0) > config.SESSION_MAX_AGE_S:
                sess.clear()
                return RedirectResponse(f"{config.LOGIN_PATH}?err=expired", status_code=303)
            request.state.user = user
            # per-user workspaces (v116): whatever this request creates belongs to the view's
            # owner — the user, or the workspace the super admin is looking at
            try:
                from . import ctx as _ctx, scope as _scope, users as _users
                owner, view = user.id, user.id
                if _users.is_owner(user):
                    picked = _scope.parse_cookie(request.cookies.get(_scope.COOKIE))
                    owner = picked if picked is not None else user.id
                    view = picked                       # None = Everyone
                else:
                    picked = _scope.parse_cookie(request.cookies.get(_scope.COOKIE))
                    if picked is not None and picked in _users.viewable_ids(user):     # v155.21 "Admin" grant
                        owner = view = picked
                _ctx.OWNER.set(owner)
                _ctx.VIEW.set(view)
            except Exception:  # noqa: BLE001
                pass
            # 2FA is mandatory: until it's set up, only the setup page (and logout) is reachable
            if config.REQUIRE_2FA and not user.totp_secret and not (path.startswith("/login/2fa/setup") or path == "/logout"):
                return RedirectResponse(f"{config.LOGIN_PATH}/2fa/setup", status_code=303)
            # the PIN gate (SECURITY_PIN): the most sensitive changes ask for it once per session
            from . import security_gate as _gate
            if _gate.pin_needed(request.method, path, bool(sess.get("pin_ok"))):
                back = _gate.safe_next(__import__("urllib.parse", fromlist=["urlparse"]).urlparse(request.headers.get("referer", "")).path or "/settings")
                if request.headers.get("x-requested-with") == "fetch":
                    from fastapi.responses import JSONResponse as _J
                    return _J({"ok": False, "error": "Enter the security PIN first.", "unlock": f"/security/unlock?next={back}"}, status_code=403)
                return RedirectResponse(f"/security/unlock?next={back}", status_code=303)
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
    # audit trail: every state-changing request by a signed-in user (audit.py) — off the event loop
    try:
        from . import audit as _audit
        _u = getattr(request.state, "user", None)
        if _audit.should_record(request.method, path, _u is not None):
            _audit.submit(_u, sec.client_ip(request), request.method, path, resp.status_code)
    except Exception:  # noqa: BLE001
        pass
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
# numbered data migrations (v153): each applied exactly once, recorded in schema_migrations
try:
    from . import migrations as _migrations
    from .database import engine as _engine
    _migrations.run(_engine)
except Exception:  # noqa: BLE001 — never keep the app from starting
    _logging.getLogger("adops.migrations").exception("migrations did not run")
if config.MOCK_TIKTOK:
    _logging.getLogger("adops").warning("TikTok TEST MODE is ON — every TikTok call is simulated locally (ADOPS_MOCK_TIKTOK=1)")
elif config.MOCK_TIKTOK_ASKED:
    _logging.getLogger("adops").error("ADOPS_MOCK_TIKTOK=1 is set on a deployed server and was IGNORED — test mode is for local development only")
try:
    from .database import seal_secrets as _seal
    _seal()                                   # v148: tokens / 2FA secrets / cookie files sealed at rest
except Exception:  # noqa: BLE001
    pass
# first start: turn APP_PASSWORD into the owner account (see users.bootstrap)
try:
    from . import users as _users
    from .database import SessionLocal as _BootSL
    _d = _BootSL()
    try:
        _users.bootstrap(_d)
        _users.ensure_owner(_d)      # OWNER_EMAIL set later / renamed / locked out → still an owner you can log in as
        from . import scope as _scope
        _scope.backfill(_d)          # v116: everything made before per-user workspaces belongs to the super admin
        from . import settings_store as _ss
        _ss.get_settings(_d)         # v119: the pre-workspaces settings row (and its postback key) becomes the super admin's
        # v155.36: every (re)start on the record — an OOM kill or a crash shows as a boot line with
        # no "stopped" before it, so Diagnostics › Uptime can say WHEN the site went down and what
        # the last memory breadcrumb was
        try:
            import os as _os
            from . import queries as _q
            _q.log(_d, f"app started: build {config.build_id()} pid {_os.getpid()} · memory limit {background.mem_limit_mb() or '?'} MB",
                   level="info", source="boot")
        except Exception:  # noqa: BLE001
            pass
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
        try:
            from . import launch_trace as _lt, models as _m
            from .routes.campaigns import MAYBE_CREATED_MARK as _mark
            _lt.recover(_db, _m, _mark)                # launches this restart killed → say what exists
            # a queued launch the restart caught mid-run: never re-run blindly (its trace above
            # says whether a campaign exists)
            _db.query(_m.LaunchQueueItem).filter(_m.LaunchQueueItem.status == "running").update(
                {_m.LaunchQueueItem.status: "failed",
                 _m.LaunchQueueItem.last_error: "interrupted by a restart — check the launch result before queueing it again"},
                synchronize_session=False)
            _db.commit()
        except Exception:  # noqa: BLE001
            _db.rollback()
        try:
            from .routes.creatives import recover_stuck_ai, resume_hf
            resume_hf(_db)                             # Higgsfield renders kept going — pick their pollers back up
            recover_stuck_ai(_db, max_age_min=0)       # a restart killed every Gemini thread — say so on their tiles
            from . import video_caption as _vc, models as _vm
            _vc.recover(_db, _vm, older_than_min=0)    # …and every caption burn in progress
            from . import asset_builds as _ab
            _ab.recover(_db, _vm)                      # …and every page / form build
        except Exception:  # noqa: BLE001
            pass
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
          ad_texts.router, locations.router, escape_test.router, tracking_routes.router, landers_page.router,
          appeals_page.router, partners_page.router, bc_assets_page.router, diagnostics.router, jobs_page.router, audience.router, display_cards.router, notes.router, pnl_page.router, assistant_page.router, creators.router, pub.router, team.router,
          warmup_page.router, lab_page.router, asset_builds_page.router):
    app.include_router(r)
