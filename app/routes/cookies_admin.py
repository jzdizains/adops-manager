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


@router.get("/cookies")
def cookies_page(request: Request, db: Session = Depends(get_db)):
    # ONE TikTok web session serves every workspace's web-API calls — only the owner manages it
    if not guard.is_owner(request):
        return RedirectResponse("/settings?err=" + guard.OWNER_ONLY_MSG.replace(" ", "+"), status_code=303)
    own = db.query(models.AdAccount).filter(models.AdAccount.enabled == True).first()  # noqa: E712
    health = spark_web_api.probe_health(own.advertiser_id if own else None)
    stored = spark_web_api.load_cookies()
    return render(request, "cookies_admin.html", {
        "title": "TikTok Cookies",
        "health": health,
        "saved_at": spark_web_api.cookies_saved_at(),
        "cookie_names": sorted(stored.keys()),
        "region": spark_web_api.session_region(stored),
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
    })


@router.post("/cookies/save")
def save(request: Request, raw: str = Form(...)):
    if not guard.is_owner(request):
        return RedirectResponse("/settings?err=" + guard.OWNER_ONLY_MSG.replace(" ", "+"), status_code=303)
    try:
        verdict = spark_web_api.save_cookies(raw)
        from .. import audit, models as _m
        from ..database import SessionLocal as _SL
        _d = _SL()
        try:
            audit.from_request(_d, _m, request, "cookies.saved", detail=f"{verdict['family']} family")
        finally:
            _d.close()
        return RedirectResponse(f"/cookies?ok=Saved+({verdict['family']}+family)", status_code=303)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse(f"/cookies?err={str(e)[:200]}", status_code=303)


@router.post("/cookies/push")
async def push(request: Request):
    """JSON endpoint the companion Chrome extension POSTs to."""
    if not guard.is_owner(request):
        return JSONResponse({"ok": False, "error": "Only the workspace owner can replace the TikTok session."},
                            status_code=403)
    body = await request.json()
    raw = body.get("cookies", "")
    if isinstance(raw, (list, dict)):
        import json as _json
        raw = _json.dumps(raw)
    try:
        verdict = spark_web_api.save_cookies(raw)
        return {"ok": True, "family": verdict["family"]}
    except spark_web_api.WebAuthError as e:
        return {"ok": False, "error": str(e)}
