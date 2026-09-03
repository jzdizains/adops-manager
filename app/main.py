"""FastAPI app — registers the routers, session middleware, auth gate, static."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import background, config
from .database import init_db
from .routes import (
    ad_texts, alerts, auth, automation, campaigns, cookies_admin, creatives, dashboard,
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
    return await call_next(request)


# Added AFTER the login middleware so SessionMiddleware sits OUTERMOST and has
# populated request.session before the login check runs (Starlette ordering).
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET,
                   session_cookie="adops_session", max_age=14 * 24 * 3600)


# Create tables / run light migrations at import time — robust under uvicorn,
# TestClient, and one-off scripts alike.
init_db()

# Background worker: balance sync + low-balance alerts + metric refresh.
background.start()


for r in (auth.router, security.router, oauth.router, dashboard.router,
          templates_routes.router, campaigns.router, super_launcher.router,
          status.router, performance.router, spark_codes.router,
          instant_pages.router, lead_forms.router, cookies_admin.router,
          monitor.router, alerts.router, inbox.router,
          settings_page.router, postback.router, pixels.router,
          automation.router, issues_page.router, creatives.router,
          ad_texts.router, locations.router, escape_test.router):
    app.include_router(r)
