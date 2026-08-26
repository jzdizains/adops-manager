"""TikTok Cookies page — paste a Cookie-Editor export (or push from the
companion Chrome extension) to refresh the web-call session; shows a health
verdict (live / live-no-permission / expired / missing — §9.6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, spark_web_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/cookies")
def cookies_page(request: Request, db: Session = Depends(get_db)):
    own = db.query(models.AdAccount).filter(models.AdAccount.enabled == True).first()  # noqa: E712
    health = spark_web_api.probe_health(own.advertiser_id if own else None)
    stored = spark_web_api.load_cookies()
    return render(request, "cookies_admin.html", {
        "title": "TikTok Cookies",
        "health": health,
        "saved_at": spark_web_api.cookies_saved_at(),
        "cookie_names": sorted(stored.keys()),
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
    })


@router.post("/cookies/save")
def save(raw: str = Form(...)):
    try:
        verdict = spark_web_api.save_cookies(raw)
        return RedirectResponse(f"/cookies?ok=Saved+({verdict['family']}+family)", status_code=303)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse(f"/cookies?err={str(e)[:200]}", status_code=303)


@router.post("/cookies/push")
async def push(request: Request):
    """JSON endpoint the companion Chrome extension POSTs to."""
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
