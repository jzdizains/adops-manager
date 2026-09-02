"""/inbox — the unified notification center (see app/inbox.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import inbox as inbox_mod, models
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/inbox")
def inbox_page(request: Request, db: Session = Depends(get_db)):
    level = request.query_params.get("level", "all")        # all | err | warn | info
    items = inbox_mod.build(db)
    counts = inbox_mod.counts(items)
    if level in ("err", "warn", "info"):
        items = [i for i in items if i["level"] == level]
    return render(request, "inbox.html", {
        "title": "Inbox", "items": items, "counts": counts, "level": level,
    })


@router.post("/inbox/dismiss")
async def dismiss(request: Request, db: Session = Depends(get_db)):
    """Dismiss one dismissable item (alert-backed) or all of them."""
    form = await request.form()
    item_id = (form.get("id") or "").strip()
    nxt = form.get("next") or "/inbox"
    if item_id == "all":
        db.query(models.Alert).filter_by(acknowledged=False).update({"acknowledged": True})
        db.commit()
    elif item_id.startswith("alert:"):
        a = db.get(models.Alert, int(item_id.split(":", 1)[1]))
        if a:
            a.acknowledged = True
            db.commit()
    return RedirectResponse(nxt, status_code=303)
