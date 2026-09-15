"""Super admin: switch the view (POST /view) and the Team page — P&L per user.

/view sets one cookie (scope.COOKIE) and sends the super admin back where they were;
anyone else is bounced (a buyer's view is always their own). /team lists every user
with their accounts, live campaigns and the range's spend / revenue / profit — the
same numbers their own Home shows — and a button to open that user's view.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import models, pnl_data, scope as scope_mod, templating, timeutil, users
from ..database import get_db
from ..templating import render

router = APIRouter()


def _back(request: Request) -> str:
    """Where to send the browser after the switch: the page it came from, path only
    (never another host)."""
    from urllib.parse import urlparse
    ref = urlparse(request.headers.get("referer") or "")
    path = ref.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    return path + (f"?{ref.query}" if ref.query else "")


@router.post("/view")
def set_view(request: Request, value: str = Form("all"), db=Depends(get_db)):
    me = getattr(request.state, "user", None)
    if not users.is_owner(me):
        return RedirectResponse(_back(request), status_code=303)
    value = (value or "all").strip()
    if value != "all":
        uid = scope_mod.parse_cookie(value)
        u = db.get(models.User, uid) if uid is not None else None
        if u is None:
            value = "all"
    resp = RedirectResponse(_back(request), status_code=303)
    resp.set_cookie(scope_mod.COOKIE, value, max_age=scope_mod.COOKIE_MAX_AGE, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    templating.forget_view_cache()
    return resp


@router.get("/team")
def team_page(request: Request, db=Depends(get_db)):
    me = getattr(request.state, "user", None)
    if not users.is_owner(me):
        return RedirectResponse("/", status_code=303)
    range_key = request.query_params.get("range", "today")
    if range_key not in ("today", "yesterday", "7d", "30d", "mtd"):
        range_key = "today"
    start_utc, end_utc = timeutil.range_bounds(range_key)
    people = db.query(models.User).order_by(models.User.email).all()
    owner_of = {aid: uid for aid, uid in db.query(models.AdAccount.advertiser_id, models.AdAccount.owner_user_id)}
    live_by_adv: dict[str, int] = {}
    for adv, in db.query(models.CampaignRecord.advertiser_id).filter(models.CampaignRecord.operation_status == "ENABLE"):
        live_by_adv[adv] = live_by_adv.get(adv, 0) + 1
    rows = []
    for u in people:
        ids = {aid for aid, uid in owner_of.items() if uid == u.id}
        k = pnl_data.overall_totals(db, start_utc, end_utc, ids)
        rows.append({"user": u, "accounts": len(ids), "live": sum(live_by_adv.get(a, 0) for a in ids),
                     "spend": k["spend"], "revenue": k["revenue"], "profit": k["profit"], "roas": k["roas"],
                     "conversions": k["conversions"], "is_me": u.id == me.id})
    rows.sort(key=lambda r: r["profit"], reverse=True)
    unowned = sum(1 for uid in owner_of.values() if uid is None)
    total = pnl_data.overall_totals(db, start_utc, end_utc)
    return render(request, "team.html", {
        "title": "Team", "rows": rows, "range_key": range_key, "unowned": unowned, "total": total,
        "labels": {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days", "30d": "Last 30 days", "mtd": "Month to date"},
    })
