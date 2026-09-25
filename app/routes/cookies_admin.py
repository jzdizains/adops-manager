"""TikTok Cookies page — paste a Cookie-Editor export (or push from the
companion Chrome extension) to refresh the web-call session; shows a health
verdict (live / live-no-permission / expired / missing — §9.6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models, spark_web_api
from ..database import get_db
from ..templating import render
from . import guard

router = APIRouter()


def _who(request: Request):
    """(user, is_owner, workspace user id) — the workspace in view decides whose session this is."""
    from .. import ctx
    me = getattr(getattr(request, "state", None), "user", None)
    owner = guard.is_owner(request)
    return me, owner, (ctx.OWNER.get() if ctx.OWNER.get() is not None else getattr(me, "id", None))


@router.get("/cookies")
def cookies_page(request: Request, db: Session = Depends(get_db)):
    """v155.38: every user manages the TikTok web session of their OWN workspace. The owner's is the
    shared default; a member without one of their own borrows it until they paste theirs."""
    from .. import scope as scope_mod
    me, owner, uid = _who(request)
    if me is None:
        return RedirectResponse("/login", status_code=303)
    sc = scope_mod.for_request(request, db)
    shared = owner and (sc.everything or sc.user_id == me.id)     # the owner on Everyone / Mine edits the shared session
    own = None
    for a in db.query(models.AdAccount).filter(models.AdAccount.enabled == True).order_by(models.AdAccount.advertiser_name):  # noqa: E712
        if sc.allows(a.advertiser_id):
            own = a
            break
    stored = spark_web_api.load_cookies(None if shared else uid)
    health = spark_web_api.probe_health(own.advertiser_id if own else None)
    return render(request, "cookies_admin.html", {
        "title": "TikTok Cookies",
        "health": health,
        "saved_at": spark_web_api.cookies_saved_at(None if shared else uid),
        "cookie_names": sorted(stored.keys()),
        "region": spark_web_api.session_region(stored),
        "shared": shared, "borrowing": (not shared) and not spark_web_api.has_own_cookies(uid),
        "whose": ("the shared session (every workspace without one of its own uses it)" if shared else "this workspace's own session"),
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
    })


@router.post("/cookies/save")
def save(request: Request, raw: str = Form(...), db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    me, owner, uid = _who(request)
    if me is None:
        return RedirectResponse("/login", status_code=303)
    sc = scope_mod.for_request(request, db)
    shared = owner and (sc.everything or sc.user_id == me.id)
    try:
        verdict = spark_web_api.save_cookies(raw, user_id=uid, shared=shared)
        from .. import audit, models as _m
        audit.from_request(db, _m, request, "cookies.saved", detail=f"{verdict['family']} family · {'shared' if shared else 'user ' + str(uid)}")
        return RedirectResponse(f"/cookies?ok=Saved+({verdict['family']}+family)", status_code=303)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse(f"/cookies?err={str(e)[:200]}", status_code=303)


@router.post("/cookies/push")
async def push(request: Request):
    """JSON endpoint the companion Chrome extension POSTs to — into the session of the
    workspace the logged-in user is in (the owner on Everyone / Mine: the shared one)."""
    from .. import scope as scope_mod
    from ..database import SessionLocal as _SL
    me, owner, uid = _who(request)
    if me is None:
        return JSONResponse({"ok": False, "error": "Sign in first."}, status_code=403)
    _d = _SL()
    try:
        sc = scope_mod.for_request(request, _d)
    finally:
        _d.close()
    shared = owner and (sc.everything or sc.user_id == me.id)
    body = await request.json()
    raw = body.get("cookies", "")
    if isinstance(raw, (list, dict)):
        import json as _json
        raw = _json.dumps(raw)
    try:
        verdict = spark_web_api.save_cookies(raw, user_id=uid, shared=shared)
        return {"ok": True, "family": verdict["family"]}
    except spark_web_api.WebAuthError as e:
        return {"ok": False, "error": str(e)}
