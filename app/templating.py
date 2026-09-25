"""Shared Jinja2 environment + render helper (adds globals every page needs)."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config, nav, users

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals.update({
    "APP_NAME": config.APP_NAME,
    "STATIC_V": config.STATIC_VERSION,
    "BUILD_ID": config.build_id,       # callable: fingerprint of the running code
    "login_path": config.LOGIN_PATH,
    "TZ_NAME": config.BUSINESS_TZ,
    "MOCK_TIKTOK": config.MOCK_TIKTOK,     # TikTok test mode banner (local development only)
    # Ads Manager deep link for one ad account (verified aadvid format) — every
    # account name/id shown in a table links here, in a new tab.
    "ads_manager_url": lambda advertiser_id: f"https://ads.tiktok.com/i18n/dashboard?aadvid={advertiser_id}",
    # shell navigation (see nav.py): sidebar items, section tabs, ⌘K jump list
    "nav_sidebar": nav.sidebar,
    "nav_footer": nav.footer,
    "nav_tabs": nav.tabs,
    "nav_jump": [list(j) for j in nav.JUMP],
    "is_owner": users.is_owner,
    # a failed launch → the page where it can be fixed (launch result cards)
    "fix_for": __import__("app.error_messages", fromlist=["fix_for"]).fix_for,
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


def _strip_key(q):
    from .routes.postback import strip_key
    return strip_key(q or "")


def _js(json_text):
    """A JSON string made safe inside <script>: "</script>" in user data (a preset's ad text, a
    Business Center name) can't end the block and run as HTML. Unicode escapes are still valid JSON."""
    import json as _json
    from markupsafe import Markup
    if json_text is None:
        t = "null"
    elif isinstance(json_text, str):
        t = json_text                     # already JSON text (the *_json values)
    else:
        # a dict / list straight from the route — encode it here. Before v155.3 it went out as
        # Python's repr ({'a': 'b'}), which JSON.parse rejects: the Clone pop-up's "Has it" and
        # the Team drawer silently read nothing.
        t = _json.dumps(json_text, default=str, ensure_ascii=False)
    for a, b in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        t = t.replace(a, b)
    return Markup(t)


templates.env.filters.update({"local": _local, "ago": _ago, "money": _money, "strip_key": _strip_key, "js": _js})


_VIEW_CACHE: dict = {}          # cookie value -> (expires, switch options) for the super admin's top bar
_VIEW_TTL_S = 20


def view_switch(request: Request) -> dict | None:
    """The top-bar "Viewing" control (super admin only): current label + options.
    One tiny users query, remembered 20 s per cookie value so pollers never hit the DB."""
    import time as _time
    me = getattr(getattr(request, "state", None), "user", None)
    if me is None or not (users.is_owner(me) or users.viewable_ids(me)):
        return None
    from . import scope as _scope
    from .database import SessionLocal
    key = (me.id, request.cookies.get(_scope.COOKIE, ""))
    hit = _VIEW_CACHE.get(key)
    if hit and hit[0] > _time.time():
        return hit[1]
    db = SessionLocal()
    try:
        sc = _scope.current(request, db, me)
        out = {"label": sc.label, "mode": sc.mode, "options": _scope.switch_options(db, sc), "user_id": sc.user_id,
               "owner": users.is_owner(me)}
    finally:
        db.close()
    if len(_VIEW_CACHE) > 200:
        _VIEW_CACHE.clear()
    _VIEW_CACHE[key] = (_time.time() + _VIEW_TTL_S, out)
    return out


def forget_view_cache() -> None:
    _VIEW_CACHE.clear()


OWNER_ONLY_JUMP = frozenset({"/settings#users", "/settings#access", "/team", "/partners"})


def render(request: Request, name: str, ctx: dict | None = None):
    ctx = dict(ctx or {})
    ctx["request"] = request
    ctx.setdefault("title", config.APP_NAME)
    ctx.setdefault("active", request.url.path)
    try:
        ctx.setdefault("view_switch", view_switch(request))
    except Exception:      # noqa: BLE001 — the switch must never break a page
        ctx.setdefault("view_switch", None)
    # ⌘K: the owner-only settings tabs (Users, Access log) exist only on the owner's own
    # view — everyone else, and the owner while switched to a user, must not be offered them
    me = getattr(request.state, "user", None)
    vs = ctx.get("view_switch") or {}
    own_view = users.is_owner(me) and (vs.get("mode") == "all" or vs.get("user_id") == getattr(me, "id", None))
    if not own_view:
        ctx.setdefault("nav_jump", [list(j) for j in nav.JUMP if j[0] not in OWNER_ONLY_JUMP])
    return templates.TemplateResponse(request, name, ctx)
