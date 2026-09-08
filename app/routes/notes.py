"""Notes on any object: GET /notes/{kind}/{ref} → {text}, POST text= → saved.
Used by the campaign drawer (kind=campaign) and, later, creatives/accounts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import activity
from ..database import get_db

router = APIRouter()
KINDS = {"campaign", "creative", "account", "preset", "spark", "bc"}


@router.get("/notes/{kind}/{ref_id}")
def get_note(kind: str, ref_id: str, db: Session = Depends(get_db)):
    if kind not in KINDS:
        return JSONResponse({"error": "unknown kind"}, status_code=400)
    n = activity.get_note(db, kind, ref_id)
    return JSONResponse({"text": (n.text if n else ""), "by": (n.by_email if n else ""),
                         "updated_at": (n.updated_at.isoformat() + "Z") if (n and n.updated_at) else ""})


@router.post("/notes/{kind}/{ref_id}")
def save_note(request: Request, kind: str, ref_id: str, text: str = Form(""), db: Session = Depends(get_db)):
    if kind not in KINDS:
        return JSONResponse({"error": "unknown kind"}, status_code=400)
    n = activity.set_note(db, kind, ref_id, text, request=request)
    return JSONResponse({"ok": True, "text": n.text, "updated_at": n.updated_at.isoformat() + "Z"})
