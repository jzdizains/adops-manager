"""PIN unlock page for the optional security gate."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import security_gate
from ..templating import render

router = APIRouter()


@router.get("/security/unlock")
def unlock_page(request: Request):
    if not security_gate.pin_enabled():
        return RedirectResponse("/", status_code=303)
    return render(request, "security_pin.html", {
        "title": "Enter PIN",
        "next": security_gate.safe_next(request.query_params.get("next", "/")),
        "error": request.query_params.get("err", ""),
    })


@router.post("/security/unlock")
def unlock_submit(request: Request, pin: str = Form(...), next: str = Form("/")):
    from urllib.parse import quote
    import time
    nxt = security_gate.safe_next(next)
    who = f"u{request.session.get('uid') or ''}|{request.client.host if request.client else ''}"
    wait = security_gate.pin_locked(who)
    if wait:
        return RedirectResponse(f"/security/unlock?next={quote(nxt)}&err=" + quote(f"Too many wrong PINs — try again in {wait // 60 + 1} min."), status_code=303)
    if security_gate.check_pin(pin):
        security_gate._FAILS.pop(who, None)
        request.session["pin_ok"] = True
        return RedirectResponse(nxt, status_code=303)
    security_gate.pin_failed(who)
    time.sleep(1.0)                       # blunts guessing (this route runs in the threadpool)
    return RedirectResponse(f"/security/unlock?next={quote(nxt)}&err=1", status_code=303)
