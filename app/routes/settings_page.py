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
        "&event=purchase"
    )   # no ttclid param: Glitchy has no macro for it — it rides inside {source}
    from pathlib import Path

    from .. import background
    pass_script = (Path(__file__).resolve().parent.parent / "static" / "pass-source.js").read_text()
    return render(request, "settings.html", {
        "title": "Settings", "s": s,
        "postback_template": postback_template, "pass_script": pass_script,
        "ok": request.query_params.get("ok", ""),
        "tz": config.BUSINESS_TZ,
        "rss_mb": background.rss_mb(),
        "web_events": STANDARD_WEB_EVENTS, "fire_max": FIRE_MAX,
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


STANDARD_WEB_EVENTS = ("Purchase", "CompleteRegistration", "ViewContent", "AddToCart", "InitiateCheckout",
                       "AddPaymentInfo", "PlaceAnOrder", "SubmitForm", "Subscribe", "StartTrial", "Contact",
                       "Search", "Download", "ClickButton")
FIRE_MAX = 25


@router.post("/settings/test-event")
async def test_event(request: Request, db: Session = Depends(get_db)):
    """Fire N events of a chosen name at the pixel — the same /event/track/
    call a real postback makes, but nothing is stored, so the P&L stays clean.
    Used to seed an event on a fresh pixel (e.g. Purchase, so it can be picked
    as an optimisation goal) or to test attribution with a real ttclid.
    ttclid is optional: without one TikTok still records the event on the
    pixel, it just can't attribute it to a click."""
    import time as _time
    from urllib.parse import quote
    form = await request.form()
    from . import postback as pb
    from .. import models, tiktok_api
    event = str(form.get("event") or "").strip() or "Purchase"
    custom = str(form.get("event_custom") or "").strip()
    if custom:
        event = custom
    event = pb.LEGACY_EVENT_NAMES.get(event, event)
    if not event.replace("_", "").isalnum() or len(event) > 50:
        return RedirectResponse("/settings?err=" + quote("Event name must be letters/digits only (e.g. Purchase)."), status_code=303)
    try:
        count = max(1, min(int(form.get("count") or 1), FIRE_MAX))
    except ValueError:
        count = 1
    ttclid = str(form.get("ttclid") or "").strip()
    source, packed = pb.unpack_source(str(form.get("source") or ""))
    ttclid = ttclid or packed          # a pasted "name~ttclid" works too
    value_raw = str(form.get("value") or "1").strip()
    try:
        value = float(value_raw)
    except ValueError:
        value = 1.0
    s = dict(get_settings(db))
    s["events_api_enabled"] = True          # a test always tries to send
    page_url = str(form.get("page_url") or "").strip() or pb.page_url_for(db, source, s)
    token, pixel_code = pb._events_pixel_code(db, source, s)
    if (s.get("events_access_token") or "").strip():
        token = s["events_access_token"].strip()
    if not token:
        return RedirectResponse("/settings?err=" + quote("Nothing sent — no token to fire with (connect TikTok or paste an Events API token)."), status_code=303)
    if not pixel_code:
        return RedirectResponse("/settings?err=" + quote("Nothing sent — no pixel to fire to. Set a Pixel ID in Events API settings or give a source that was launched."), status_code=303)
    sent = 0
    errors: list[str] = []
    stamp = int(_time.time())
    for i in range(count):
        try:
            tiktok_api.track_event(
                token, pixel_code, event=event, event_id=f"test-{stamp}-{i + 1}",
                ttclid=ttclid, value=value, currency=(s.get("events_currency") or "USD").strip(),
                test_event_code=(s.get("events_test_code") or "").strip(), page_url=page_url)
            sent += 1
        except tiktok_api.TikTokError as e:
            errors.append(f"code {e.code}: {(e.message or '')[:140]}")
            if len(errors) >= 2:            # the same refusal N times helps nobody
                break
        if count > 1:
            _time.sleep(0.25)
    where = ("the pixel's Test Events tab (test code set)" if (s.get("events_test_code") or "").strip()
             else "Events Manager → the pixel's event overview (can take a few minutes)")
    if sent and not errors:
        msg = f"Sent {sent} × {event} to pixel {pixel_code}" + (" with ttclid" if ttclid else " (no ttclid — recorded, not attributed)") + f". Check {where}."
        return RedirectResponse("/settings?ok=" + quote(msg) + "#test", status_code=303)
    msg = f"Sent {sent} of {count} × {event}" + (f" — TikTok refused: {errors[0]}" if errors else "")
    return RedirectResponse("/settings?err=" + quote(msg) + "#test", status_code=303)
