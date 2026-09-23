"""/inbox — the unified notification center (see app/inbox.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import inbox as inbox_mod, models
from ..database import get_db
from ..templating import render

router = APIRouter()

GROUP_ROW_CAP = 200      # collapsed rows drawn per group; the rest fold into a "+N more" line


def _collapse_rows(rows: list[dict], cap: int = GROUP_ROW_CAP) -> tuple[list[dict], int]:
    """Fold near-identical rows (same title + message + account) into ONE row that
    carries a `count`, so a batch of 1,041 ads all "rejected (no reason returned)"
    becomes a single line "× 1041" instead of 1,041 identical rows. Keeps the newest
    timestamp and the first row's fix action / level / kind. Returns (rows, hidden)
    where `hidden` is how many distinct collapsed rows past the cap were dropped.

    Shared by the Inbox groups and the Health (/monitor) Issues table so both fold
    the same way and neither renders thousands of DOM rows the browser then chokes on."""
    from collections import OrderedDict
    buckets: "OrderedDict[tuple, dict]" = OrderedDict()
    for it in rows:
        sig = (it.get("title", ""), it.get("message", ""), it.get("where", ""))
        b = buckets.get(sig)
        if b is None:
            nb = dict(it)
            nb["count"] = 1
            buckets[sig] = nb
        else:
            b["count"] += 1
            if it.get("at") and (b.get("at") is None or it["at"] > b["at"]):
                b["at"] = it["at"]
    collapsed = list(buckets.values())
    hidden = 0
    if len(collapsed) > cap:
        hidden = len(collapsed) - cap
        collapsed = collapsed[:cap]
    return collapsed, hidden


@router.get("/inbox")
def inbox_page(request: Request, db: Session = Depends(get_db)):
    """Grouped by kind (blocked accounts, rejections, failed launches…), each
    item with the buttons that fix it. Level filter + search happen in the
    browser; ?level= only picks the starting filter."""
    from .monitor import KIND_LABEL, _fix_actions
    level = request.query_params.get("level", "all")        # all | err | warn | info
    if level not in ("err", "warn", "info"):
        level = "all"
    from .. import scope as scope_mod
    items = inbox_mod.build(db, scope_mod.for_request(request, db))
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
    for g in ordered:
        g["total"] = len(g["rows"])                       # true number of issues in this group
        g["rows"], g["hidden"] = _collapse_rows(g["rows"])  # one line per distinct message
    return render(request, "inbox.html", {
        "title": "Inbox", "items": items, "counts": counts, "level": level, "groups": ordered,
    })


@router.post("/inbox/dismiss")
async def dismiss(request: Request, db: Session = Depends(get_db)):
    """Dismiss one dismissable item (alert-backed) or all of them."""
    form = await request.form()
    item_id = (form.get("id") or "").strip()
    nxt = form.get("next") or "/inbox"
    from .. import inbox as inbox_mod, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    if item_id == "all":                         # everything THIS person sees — never other workspaces'
        for a in inbox_mod.visible_unacked(db, sc):
            a.acknowledged = True
        db.commit()
    elif item_id.startswith("alert:") and item_id.split(":", 1)[1].isdigit():
        a = db.get(models.Alert, int(item_id.split(":", 1)[1]))
        if a and inbox_mod.alert_visible(a, sc):
            a.acknowledged = True
            db.commit()
    from .alerts import bell_cache_clear
    bell_cache_clear()
    if request.headers.get("x-requested-with") == "fetch":
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})
    return RedirectResponse(nxt, status_code=303)
