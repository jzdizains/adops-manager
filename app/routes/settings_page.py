"""/settings — every runtime-tunable knob in one place. Changes apply within
one sweep (no redeploy needed)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import config, models, queries, text_overlay, users
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
    if config.POSTBACK_HOST:
        base_url = "https://" + config.POSTBACK_HOST       # the dedicated postback hostname
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
        "classic_font": text_overlay.custom_font_status(),
        "sec": _security_ctx(request, db),
    })


@router.post("/settings/font")
async def upload_font(request: Request):
    """Store an optional custom typeface for the text-on-image tool: one
    .ttf/.otf per weight; missing weights fall back to the nearest uploaded one."""
    form = await request.form()
    saved, bad = [], []
    d = text_overlay.custom_font_dir()
    d.mkdir(parents=True, exist_ok=True)
    for weight in text_overlay.WEIGHT_KEYS:
        up = form.get(f"font_{weight}")
        if up is None or not getattr(up, "filename", ""):
            continue
        data = await up.read()
        ext = up.filename.rsplit(".", 1)[-1].lower() if "." in up.filename else ""
        ok = ext in ("ttf", "otf") and len(data) > 1000 and text_overlay.font_bytes_ok(data)
        if not ok:
            bad.append(f"{weight}: {up.filename} is not a readable TTF/OTF")
            continue
        for old in ("ttf", "otf"):
            try:
                (d / f"classic-{weight}.{old}").unlink()
            except FileNotFoundError:
                pass
        (d / f"classic-{weight}.{ext}").write_bytes(data)
        saved.append(weight)
    if form.get("remove") == "1":
        for weight in text_overlay.WEIGHT_KEYS:
            for ext in ("ttf", "otf"):
                try:
                    (d / f"classic-{weight}.{ext}").unlink()
                except FileNotFoundError:
                    pass
        return RedirectResponse("/settings?ok=" + quote("Custom font files removed — the text tool uses TikTok Sans.") + "#font", status_code=303)
    if not saved and not bad:
        return RedirectResponse("/settings?err=" + quote("Pick at least one font file.") + "#font", status_code=303)
    msg = (f"Saved {', '.join(saved)} — “Custom font” is now available in the text tool." if saved else "") \
        + ((" " if saved else "") + "; ".join(bad) if bad else "")
    return RedirectResponse(("/settings?ok=" if saved else "/settings?err=") + quote(msg) + "#font", status_code=303)


@router.get("/fonts/classic/{weight}")
def classic_font_file(weight: str):
    """Serve the uploaded Classic file so the browser preview matches the bake."""
    if weight not in text_overlay.WEIGHT_KEYS:
        return Response(status_code=404)
    p = text_overlay.font_file("classic", weight)
    if not p.exists() or "classic-" not in p.name:
        return Response(status_code=404)
    return FileResponse(str(p), media_type="font/otf" if p.suffix == ".otf" else "font/ttf",
                        headers={"Cache-Control": "no-cache"})


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


def _security_ctx(request: Request, db: Session) -> dict:
    from .. import auth_security as sec
    from .auth import current_user
    me = current_user(request, db)
    pending = request.session.get("totp_setup") or ""
    return {
        "me": me, "enabled": sec.totp_enabled(me), "enabled_at": (me.totp_enabled_at if me else "") or "",
        "recovery_left": sec.recovery_left(me) if sec.totp_enabled(me) else 0,
        "pending_secret": pending, "pending_uri": sec.otpauth_uri(pending, me.email if me else "operator") if pending else "",
        "pending_qr": sec.qr_svg(sec.otpauth_uri(pending, me.email if me else "operator")) if pending else "",
        "new_codes": request.session.pop("totp_new_codes", None),
        "recent": sec.recent_logins(db, kinds=("login", "2fa")) if not users.is_owner(me) else [],
        "access": sec.access_log(db, 80) if users.is_owner(me) else [],
        "this_ip": sec.client_ip(request), "this_where": _where(db, sec.client_ip(request)),
        "geo_on": bool(config.IPINFO_TOKEN),
        "user_where": {u.id: _where(db, u.last_ip) for u in (db.query(models.User).all() if users.is_owner(me) else [])},
        "ua": __import__("app.ua", fromlist=["parse"]),
        "allowed_ips": config.ALLOWED_IPS, "insecure": sec.insecure_defaults(),
        "session_hours": config.SESSION_MAX_AGE_S // 3600,
        "is_owner": users.is_owner(me), "owner_email": config.OWNER_EMAIL,
        "users": db.query(models.User).order_by(models.User.email).all() if users.is_owner(me) else [],
        "min_password": users.MIN_PASSWORD,
    }


def _where(db: Session, ip: str) -> str:
    from .. import geo
    try:
        return geo.label(geo.lookup(db, ip or "", [2]), ip or "")
    except Exception:  # noqa: BLE001
        return ""


def _me(request: Request, db: Session):
    from .auth import current_user
    return current_user(request, db)


def _back(ok: str = "", err: str = "") -> RedirectResponse:
    q = ("ok=" + quote(ok)) if ok else ("err=" + quote(err))
    return RedirectResponse(f"/settings?{q}#security", status_code=303)


# ---- my account: 2FA + password --------------------------------------------------------
@router.post("/settings/2fa/start")
def twofa_start(request: Request, db: Session = Depends(get_db)):
    """Step 1: make a fresh secret and show it (QR + key) until it's confirmed."""
    from .. import auth_security as sec
    me = _me(request, db)
    if sec.totp_enabled(me):
        return _back(err="Two-factor is already on — turn it off first to set up a new phone.")
    request.session["totp_setup"] = sec.new_secret()
    return RedirectResponse("/settings#security", status_code=303)


@router.post("/settings/2fa/confirm")
def twofa_confirm(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    """Step 2: the code from the app proves the phone has the secret → on."""
    from .. import auth_security as sec
    me = _me(request, db)
    secret = request.session.get("totp_setup") or ""
    if not secret or not me:
        return _back(err="Start the 2FA setup first.")
    if not sec.totp_ok(secret, code):
        return _back(err="That code didn't match — check the phone's clock and try the next code.")
    codes = sec.enable_totp(db, me, secret)
    request.session.pop("totp_setup", None)
    request.session["totp_new_codes"] = codes
    return _back(ok="Two-factor authentication is on. Save the recovery codes below now — they are shown once.")


@router.post("/settings/2fa/cancel")
def twofa_cancel(request: Request):
    request.session.pop("totp_setup", None)
    return RedirectResponse("/settings#security", status_code=303)


@router.post("/settings/2fa/disable")
def twofa_disable(request: Request, code: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    """Turning it off needs the password AND a current code (or recovery code)."""
    from .. import auth_security as sec
    me = _me(request, db)
    if not me or not users.verify_password(password, me.password_hash):
        return _back(err="Wrong password — 2FA stays on.")
    if not (sec.totp_ok(me.totp_secret, code) or sec.recovery_ok(db, me, code)):
        return _back(err="That code didn't match — 2FA stays on.")
    sec.disable_totp(db, me)
    return _back(ok="Two-factor authentication is off.")


@router.post("/settings/password")
def change_password(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                    db: Session = Depends(get_db)):
    me = _me(request, db)
    if not me or not users.verify_password(current, me.password_hash):
        return _back(err="Current password is wrong.")
    if new != confirm:
        return _back(err="The two new passwords don't match.")
    try:
        users.set_password(db, me, new)
    except ValueError as e:
        return _back(err=str(e))
    # keep THIS browser logged in on the new fingerprint; every other device is out
    request.session["fp"] = users.fingerprint(me)
    return _back(ok="Password changed. Every other device has been signed out.")


# ---- users (the OWNER_EMAIL account only) ------------------------------------------------
def _admin(request: Request, db: Session):
    me = _me(request, db)
    return me if users.is_owner(me) else None


NOT_OWNER = "Only the owner account ({}) can manage users."


@router.post("/settings/users/create")
def user_create(request: Request, email: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _back(err=NOT_OWNER.format(config.OWNER_EMAIL))
    try:
        u = users.create(db, email, password, is_admin=False, must_change=True)
    except ValueError as e:
        return _back(err=str(e))
    return _back(ok=f"User {u.email} created — they can log in with that email and the password you set, and will be asked to change it.")


@router.post("/settings/users/{user_id}/password")
def user_reset_password(user_id: int, request: Request, password: str = Form(""), db: Session = Depends(get_db)):
    if not _admin(request, db):
        return _back(err=NOT_OWNER.format(config.OWNER_EMAIL))
    u = db.get(models.User, user_id)
    if not u:
        return _back(err="No such user.")
    if users.is_owner(u):
        return _back(err="Change the owner's password from its own account.")
    try:
        users.set_password(db, u, password, by_admin=True)
    except ValueError as e:
        return _back(err=str(e))
    return _back(ok=f"New password set for {u.email}; their sessions were signed out.")


@router.post("/settings/users/{user_id}/toggle")
def user_toggle(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Deactivate / reactivate (a deactivated user is signed out at once)."""
    me = _admin(request, db)
    if not me:
        return _back(err=NOT_OWNER.format(config.OWNER_EMAIL))
    u = db.get(models.User, user_id)
    if not u:
        return _back(err="No such user.")
    if u.id == me.id or users.is_owner(u):
        return _back(err="The owner account can't be deactivated.")
    u.active = not u.active
    users.sign_out_everywhere(db, u)
    queries.log(db, f"user {'reactivated' if u.active else 'deactivated'}: {u.email}", source="auth")
    return _back(ok=f"{u.email} is now {'active' if u.active else 'deactivated'}.")


@router.post("/settings/users/{user_id}/2fa-reset")
def user_2fa_reset(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Lost phone: an admin clears the user's 2FA so they can set it up again."""
    from .. import auth_security as sec
    me = _admin(request, db)
    if not me:
        return _back(err=NOT_OWNER.format(config.OWNER_EMAIL))
    u = db.get(models.User, user_id)
    if not u:
        return _back(err="No such user.")
    if users.is_owner(u) and u.id != me.id:
        return _back(err="The owner's 2FA can only be changed from its own account.")
    sec.disable_totp(db, u)
    users.sign_out_everywhere(db, u)
    return _back(ok=f"2FA cleared for {u.email} — they'll log in with the password only until they set it up again.")


@router.post("/settings/users/{user_id}/delete")
def user_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    me = _admin(request, db)
    if not me:
        return _back(err=NOT_OWNER.format(config.OWNER_EMAIL))
    u = db.get(models.User, user_id)
    if not u:
        return _back(err="No such user.")
    if u.id == me.id or users.is_owner(u):
        return _back(err="The owner account can't be deleted.")
    email = u.email
    db.delete(u)
    db.commit()
    queries.log(db, f"user deleted: {email}", level="warn", source="auth")
    return _back(ok=f"Deleted {email}.")
