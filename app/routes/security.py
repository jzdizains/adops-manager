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
        "next": request.query_params.get("next", "/"),
        "error": request.query_params.get("err", ""),
    })


@router.post("/security/unlock")
def unlock_submit(request: Request, pin: str = Form(...), next: str = Form("/")):
    if security_gate.check_pin(pin):
        request.session["pin_ok"] = True
        return RedirectResponse(next or "/", status_code=303)
    return RedirectResponse(f"/security/unlock?next={next}&err=1", status_code=303)
