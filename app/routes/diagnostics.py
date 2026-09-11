"""/diagnostics — the error feed.

Read-only. Shows what TikTok (or the app) actually said, newest first, so a problem can
be diagnosed from the dashboard instead of from a screenshot. Sits behind the same login
as every other page; nothing here is public and nothing here contains a credential.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import diag
from ..database import get_db
from ..templating import render

router = APIRouter()

KINDS = (("", "Everything"), ("tiktok", "TikTok"), ("job", "Background jobs"), ("app", "App"))


@router.get("/diagnostics")
def diagnostics_page(request: Request, db: Session = Depends(get_db)):
    kind = request.query_params.get("kind", "")
    q = request.query_params.get("q", "")
    rows = diag.recent(db, limit=200, kind=kind, q=q)
    return render(request, "diagnostics.html", {
        "title": "Diagnostics", "active": "settings",
        "rows": [diag.as_json(r) for r in rows],
        "kind": kind, "q": q, "kinds": KINDS, "unseen": diag.unseen_count(db),
    })


@router.get("/diagnostics.json")
def diagnostics_json(request: Request, db: Session = Depends(get_db)):
    """The same feed, machine-readable — so the errors can be read and acted on
    directly rather than retyped out of a screenshot."""
    kind = request.query_params.get("kind", "")
    q = request.query_params.get("q", "")
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    rows = diag.recent(db, limit=limit, kind=kind, q=q)
    return JSONResponse({"ok": True, "count": len(rows),
                         "unseen": diag.unseen_count(db),
                         "events": [diag.as_json(r) for r in rows]})


@router.post("/diagnostics/seen")
def diagnostics_seen(db: Session = Depends(get_db)):
    diag.mark_seen(db)
    return RedirectResponse("/diagnostics?" + quote("ok=1"), status_code=303)
