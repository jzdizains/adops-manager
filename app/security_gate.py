"""Optional PIN gate for destructive/sensitive actions.

If SECURITY_PIN is set, sensitive routes call require_pin(request) and redirect
to /security/unlock until the operator enters the PIN once per session.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

from . import config


def pin_enabled() -> bool:
    return bool(config.SECURITY_PIN)


def pin_ok(request: Request) -> bool:
    return (not pin_enabled()) or bool(request.session.get("pin_ok"))


def require_pin(request: Request, next_url: str = "/") -> RedirectResponse | None:
    """Return a redirect to the unlock page if the PIN hasn't been entered."""
    if pin_ok(request):
        return None
    return RedirectResponse(f"/security/unlock?next={next_url}", status_code=303)


def check_pin(pin: str) -> bool:
    import hmac
    # bytes: compare_digest refuses non-ASCII str (a typed "é" must be a wrong PIN, not a 500)
    return pin_enabled() and hmac.compare_digest(str(pin or "").encode("utf-8"), str(config.SECURITY_PIN).encode("utf-8"))


PIN_MAX_FAILS = 5
PIN_LOCK_S = 900
_FAILS: dict = {}          # who → [failures, first failure at] (process-local, bounded)


def pin_locked(who: str) -> int:
    """Seconds this person must wait before trying the PIN again (0 = may try)."""
    import time
    n, at = _FAILS.get(who, (0, 0.0))
    left = int(at + PIN_LOCK_S - time.time())
    if n >= PIN_MAX_FAILS and left > 0:
        return left
    if left <= 0 and who in _FAILS:
        _FAILS.pop(who, None)
    return 0


def pin_failed(who: str) -> None:
    import time
    n, at = _FAILS.get(who, (0, time.time()))
    _FAILS[who] = (n + 1, at)
    if len(_FAILS) > 5000:
        for k in list(_FAILS)[:1000]:
            _FAILS.pop(k, None)


# v148: the gate is live — these POSTs need the PIN once per session when SECURITY_PIN is set
# (they change who can log in, the company's TikTok session, or the mailbox credentials)
PIN_PATHS = ("/cookies/", "/settings/users/", "/settings/2fa/disable", "/settings/security/", "/settings/sessions/",
             "/partners/mailbox", "/oauth/manual")


def pin_needed(method: str, path: str, pin_ok_now: bool) -> bool:
    """Pure: does this request have to go through the unlock page first?"""
    if not pin_enabled() or pin_ok_now or method.upper() != "POST":
        return False
    return path.startswith(PIN_PATHS)


def safe_next(v: str, default: str = "/") -> str:
    """Only a path on this site — never "//host" or "https://…" (the old gate was an open redirect)."""
    v = str(v or "")
    if not v.startswith("/") or v.startswith("//") or v.startswith("/\\") or "\n" in v or "\r" in v:
        return default
    return v
