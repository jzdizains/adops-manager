"""Shared Jinja2 environment + render helper (adds globals every page needs)."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals.update({
    "APP_NAME": config.APP_NAME,
    "STATIC_V": config.STATIC_VERSION,
    "TZ_NAME": config.BUSINESS_TZ,
})


# ---- display filters: ONE way to show times and money on every page ---------
def _local(dt, fmt: str = "%b %d · %H:%M"):
    """Naive-UTC / aware datetime (or ISO string) -> business-timezone text."""
    from datetime import datetime
    from . import timeutil
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    return timeutil.fmt_local(dt, fmt)


def _ago(dt):
    """'just now' / '4 min ago' / '3 h ago' / 'Sep 01' — for activity lists."""
    from datetime import datetime, timezone
    if not dt:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 45:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    if secs < 86400 * 7:
        return f"{int(secs // 86400)} d ago"
    return _local(dt, "%b %d")


def _money(v, digits: int = 2):
    """-2.17 -> '−$2.17', 1234.5 -> '$1,234.50' (true minus sign, thousands)."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    s = f"${abs(v):,.{digits}f}"
    return ("−" + s) if v < 0 else s


templates.env.filters.update({"local": _local, "ago": _ago, "money": _money})


def render(request: Request, name: str, ctx: dict | None = None):
    ctx = dict(ctx or {})
    ctx["request"] = request
    ctx.setdefault("title", config.APP_NAME)
    ctx.setdefault("active", request.url.path)
    return templates.TemplateResponse(request, name, ctx)
