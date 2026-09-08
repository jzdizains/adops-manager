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
    """Grouped by kind (blocked accounts, rejections, failed launches…), each
    item with the buttons that fix it. Level filter + search happen in the
    browser; ?level= only picks the starting filter."""
    from .monitor import KIND_LABEL, _fix_actions
    level = request.query_params.get("level", "all")        # all | err | warn | info
    if level not in ("err", "warn", "info"):
        level = "all"
    items = inbox_mod.build(db)
    counts = inbox_mod.counts(items)
    groups: dict[str, dict] = {}
    for it in items:
        it["fix"] = _fix_actions(it)
        g = groups.setdefault(it["kind"], {"kind": it["kind"], "label": KIND_LABEL.get(it["kind"], it["title"]), "level": it["level"], "rows": [], "fix": None})
        g["rows"].append(it)
        if it["level"] == "err":
            g["level"] = "err"
        if g["fix"] is None and it["fix"] and not it["fix"][0].get("ext"):
            g["fix"] = it["fix"][0]
    ordered = sorted(groups.values(), key=lambda g: ({"err": 0, "warn": 1, "info": 2}[g["level"]], -len(g["rows"])))
    return render(request, "inbox.html", {
        "title": "Inbox", "items": items, "counts": counts, "level": level, "groups": ordered,
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
    if request.headers.get("x-requested-with") == "fetch":
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})
    return RedirectResponse(nxt, status_code=303)
