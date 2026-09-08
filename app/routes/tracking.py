"""Public click endpoints (outside the login wall, like /postback):

  GET  /t/c?to=<lander>&source=…&ttclid=…&tt_cid=…   redirect tracking: record, then 302 to the lander
  POST /t/click   (text/plain JSON body, CORS *)       direct tracking: the lander script registers the click

Both answer with the click id; the script packs it behind the source (name~clickid) on every offer link.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import tracking
from ..database import get_db

router = APIRouter()

CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "86400", "Cache-Control": "no-store"}


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or ""


@router.options("/t/click")
def click_preflight():
    return Response(status_code=204, headers=CORS)


@router.post("/t/click")
async def click_register(request: Request, db: Session = Depends(get_db)):
    raw = (await request.body())[:4000]
    try:
        d = json.loads(raw.decode("utf-8", "ignore") or "{}")
        if not isinstance(d, dict):
            d = {}
    except ValueError:
        d = {}
    source = str(d.get("source") or "")
    ttclid = str(d.get("ttclid") or "")
    if not source and not ttclid:
        return JSONResponse({"ok": False, "error": "no source / ttclid"}, status_code=400, headers=CORS)
    row = tracking.record_click(db, source=source, ttclid=ttclid, tt_campaign_id=str(d.get("tt_cid") or ""),
                                tt_adgroup_id=str(d.get("tt_aid") or ""), tt_ad_id=str(d.get("tt_ad") or ""),
                                ip=_ip(request), user_agent=request.headers.get("user-agent", ""),
                                url=str(d.get("url") or ""), referrer=str(d.get("ref") or ""), how="direct")
    db.commit()
    return JSONResponse({"ok": True, "click_id": row.click_id}, headers=CORS)


@router.get("/t/c")
def click_redirect(request: Request, db: Session = Depends(get_db)):
    q = dict(request.query_params)
    to = (q.pop("to", "") or "").strip()
    if not to or not tracking.allowed_redirect(db, to):
        return PlainTextResponse("Unknown destination.", status_code=404)
    source = q.get("source") or ""
    row = tracking.record_click(db, source=source, ttclid=q.get("ttclid") or "", tt_campaign_id=q.get("tt_cid") or "",
                                tt_adgroup_id=q.get("tt_aid") or "", tt_ad_id=q.get("tt_ad") or "",
                                ip=_ip(request), user_agent=request.headers.get("user-agent", ""),
                                url=to, referrer=request.headers.get("referer", ""), how="redirect")
    db.commit()
    # everything the ad sent rides on to the lander, plus our click id (packed behind the source AND
    # as clid= so the lander script picks it up without registering a second click)
    fwd = {k: v for k, v in q.items() if not k.startswith("__")}
    if row.source:
        fwd["source"] = row.source + tracking.SEP + row.click_id
    fwd["clid"] = row.click_id
    return RedirectResponse(tracking.with_params(to, fwd), status_code=302, headers={"Cache-Control": "no-store"})
