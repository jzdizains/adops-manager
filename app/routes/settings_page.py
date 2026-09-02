"""/settings — every runtime-tunable knob in one place. Changes apply within
one sweep (no redeploy needed)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import config
from ..database import get_db
from ..settings_store import get_settings, save_settings
from ..templating import render

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    s = get_settings(db)
    base_url = str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
        base_url = "https://" + base_url[len("http://"):]
    postback_template = (
        f"{base_url}/postback?key={s['postback_key']}"
        "&source={source}&revenue={payout}&txn={transaction_id}"
        "&event=purchase&ttclid={ttclid}"
    )
    from pathlib import Path

    from .. import background
    pass_script = (Path(__file__).resolve().parent.parent / "static" / "pass-source.js").read_text()
    return render(request, "settings.html", {
        "title": "Settings", "s": s,
        "postback_template": postback_template, "pass_script": pass_script,
        "ok": request.query_params.get("ok", ""),
        "tz": config.BUSINESS_TZ,
        "rss_mb": background.rss_mb(),
    })


@router.post("/settings/save")
async def save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    current = get_settings(db)
    values = dict(current)
    for key in current:
        if key == "postback_key":
            continue  # never editable from the form
        if isinstance(current[key], bool):
            values[key] = form.get(key) is not None          # checkbox present = on
        elif key in form:
            values[key] = form.get(key)
    save_settings(db, values)
    return RedirectResponse("/settings?ok=Saved.+Changes+apply+within+one+sweep.", status_code=303)


@router.post("/settings/test-event")
async def test_event(request: Request, db: Session = Depends(get_db)):
    """Fire ONE CompleteRegistration (or whatever event is configured) at the
    pixel — same code path a real postback uses, but nothing is stored, so the
    P&L stays clean. Works even while forwarding is switched off."""
    from urllib.parse import quote
    form = await request.form()
    ttclid = str(form.get("ttclid") or "").strip()
    source = str(form.get("source") or "").strip()
    value = str(form.get("value") or "1")
    if not ttclid:
        return RedirectResponse("/settings?ok=" + quote(
            "Test NOT sent — paste a ttclid first (click your own ad and copy "
            "it from the landing URL)."), status_code=303)
    from .. import models
    from . import postback as pb
    s = dict(get_settings(db))
    s["events_api_enabled"] = True          # a test always tries to send
    ev = models.PostbackEvent(              # transient — never added to the DB
        source=source, ttclid=ttclid[:500],
        revenue=float(value or 1) if value.replace(".", "", 1).isdigit() else 1.0,
        txn=f"test-{__import__('time').time():.0f}")
    status = pb._forward_to_tiktok(db, ev, s)
    label = ("Test event SENT — check Events Manager (Test Events tab if you set "
             "a test code, otherwise the pixel's event overview)."
             if status == "sent" else f"Test event: {status}")
    return RedirectResponse("/settings?ok=" + quote(label), status_code=303)
