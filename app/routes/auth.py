"""App login — a single operator password, itsdangerous-signed session cookie
(via Starlette's SessionMiddleware, which signs with itsdangerous)."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import config
from ..templating import render

router = APIRouter()

# /postback is public so Glitchy's servers can reach it (auth = its key param)
PUBLIC_PATHS = ("/login", "/static", "/health", "/oauth/callback", "/favicon.ico",
                "/postback", "/t/escape")   # /t/escape/* = phone-side test pages (results stay behind login)


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", {"title": "Log in", "error": request.query_params.get("err", "")})


@router.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    if password == config.APP_PASSWORD:
        request.session["authed"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?err=1", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/health")
def health():
    return {"ok": True}
