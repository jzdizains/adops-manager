"""/display-cards — upload (from the preset form, via fetch) / list / delete."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from .. import display_cards as DC
from .. import models
from ..database import get_db

router = APIRouter()


def _safe_next(v: str) -> str:
    return v if v.startswith("/") and not v.startswith("//") else "/presets"


@router.post("/display-cards/upload")
async def upload(request: Request, db: Session = Depends(get_db)):
    """Multipart: file (image), name (optional). Returns JSON for the preset
    form's inline uploader; a plain form post (no Accept: application/json)
    redirects back with a toast."""
    form = await request.form()
    f = form.get("file")
    wants_json = "application/json" in (request.headers.get("accept") or "")
    nxt = _safe_next(str(form.get("next") or "/presets"))
    if not isinstance(f, UploadFile) or not f.filename:
        msg = "Pick an image file first."
        return JSONResponse({"error": msg}, status_code=422) if wants_json else \
            RedirectResponse(f"{nxt}?err={quote(msg)}", status_code=303)
    data = await f.read(DC.MAX_UPLOAD + 1)
    try:
        card, resized = DC.add(db, data, f.filename, str(form.get("name") or ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422) if wants_json else \
            RedirectResponse(f"{nxt}?err={quote(str(e))}", status_code=303)
    note = f"Saved display card “{card.name}”" + (" (scaled and cropped to 750×421)." if resized else ".")
    if wants_json:
        return JSONResponse({"id": card.id, "name": card.name, "resized": resized, "message": note})
    return RedirectResponse(f"{nxt}?ok={quote(note)}", status_code=303)


@router.get("/display-cards/{card_id}/image")
def image(card_id: int, db: Session = Depends(get_db)):
    card = db.get(models.DisplayCard, card_id)
    if not card or not card.file_path or not Path(card.file_path).exists():
        return Response(status_code=404)
    return FileResponse(card.file_path, media_type="image/png")


@router.post("/display-cards/{card_id}/delete")
def delete(card_id: int, request: Request, db: Session = Depends(get_db)):
    card = db.get(models.DisplayCard, card_id)
    nxt = _safe_next(request.query_params.get("next", "/presets"))
    if not card:
        return RedirectResponse(f"{nxt}?err={quote('That display card is already gone.')}", status_code=303)
    import json
    used = []
    for t in db.query(models.Template).all():
        try:
            if json.loads(t.adgroup_settings or "{}").get("display_card_id") == card.id:
                used.append(t.name)
        except ValueError:
            pass
    if used:
        return RedirectResponse(f"{nxt}?err=" + quote(
            f"“{card.name}” is used by preset(s) {', '.join(used[:5])} — pick another card there first."), status_code=303)
    name = card.name
    DC.remove(db, card)
    return RedirectResponse(f"{nxt}?ok={quote(f'Removed display card “{name}”.')}", status_code=303)
