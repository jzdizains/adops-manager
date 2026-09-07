"""FastAPI app — registers the routers, session middleware, auth gate, static."""
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import background, config
from .database import init_db
from .routes import (
    ad_texts, alerts, appeals_page, audience, auth, automation, campaigns, jobs_page, partners_page, cookies_admin, creatives, dashboard, display_cards,
    inbox, instant_pages, issues_page, lead_forms, locations, monitor, oauth,
    performance, pixels, postback, security, settings_page, spark_codes, escape_test,
    status, super_launcher, templates_routes,
)

app = FastAPI(title=config.APP_NAME, docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(auth.PUBLIC_PATHS) and not request.session.get("authed"):
        return RedirectResponse(f"/login?next={path}", status_code=303)
    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001 — turn a bare "Internal Server Error" into something actionable
        return _error_page(request, exc)


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
                   session_cookie="adops_session", max_age=14 * 24 * 3600)


# Create tables / run light migrations at import time — robust under uvicorn,
# TestClient, and one-off scripts alike.
init_db()

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
          ad_texts.router, locations.router, escape_test.router,
          appeals_page.router, partners_page.router, jobs_page.router, audience.router, display_cards.router):
    app.include_router(r)
