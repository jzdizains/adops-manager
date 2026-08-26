"""Shared Jinja2 environment + render helper (adds globals every page needs)."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals.update({
    "APP_NAME": config.APP_NAME,
    "STATIC_V": config.STATIC_VERSION,
})


def render(request: Request, name: str, ctx: dict | None = None):
    ctx = dict(ctx or {})
    ctx["request"] = request
    ctx.setdefault("title", config.APP_NAME)
    ctx.setdefault("active", request.url.path)
    return templates.TemplateResponse(request, name, ctx)
