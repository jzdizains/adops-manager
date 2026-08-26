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
    return pin_enabled() and pin == config.SECURITY_PIN
