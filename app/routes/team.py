"""Super admin: switch the view (POST /view) and the Team page — P&L per user.

/view sets one cookie (scope.COOKIE) and sends the super admin back where they were;
anyone else is bounced (a buyer's view is always their own).

/team lists every user with the range's spend / revenue / profit (the same numbers
their own Home shows), the change against the previous period of the same length,
ROAS, CPA, share of company spend, launches, live campaigns and accounts, with a
button to open that user's view. A row expands (one small JSON call) into the
user's top sources and accounts for the range.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import models, pnl_data, scope as scope_mod, templating, timeutil, users
from ..database import get_db
from ..templating import render

router = APIRouter()
RANGES = ("today", "yesterday", "7d", "30d", "mtd", "custom")


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


def _range(request: Request):
    range_key = request.query_params.get("range", "today")
    if range_key not in RANGES:
        range_key = "today"
    start, end = request.query_params.get("start") or None, request.query_params.get("end") or None
    s, e = timeutil.range_bounds(range_key, start, end)
    return range_key, s, e


def _pct(cur: float, prev: float):
    return ((cur - prev) / abs(prev) * 100) if prev else None


@router.get("/team")
def team_page(request: Request, db=Depends(get_db)):
    me = getattr(request.state, "user", None)
    if not users.is_owner(me):
        return RedirectResponse("/", status_code=303)
    range_key, start_utc, end_utc = _range(request)
    length = end_utc - start_utc
    p_start, p_end = start_utc - length, start_utc
    s_naive = start_utc.replace(tzinfo=None)
    people = db.query(models.User).order_by(models.User.email).all()
    owner_of = {aid: uid for aid, uid in db.query(models.AdAccount.advertiser_id, models.AdAccount.owner_user_id)}
    live_by_adv: dict[str, int] = {}
    clicks_by_adv: dict[str, int] = {}
    for adv, st, clicks in db.query(models.CampaignRecord.advertiser_id, models.CampaignRecord.operation_status, models.CampaignRecord.clicks):
        if st == "ENABLE":
            live_by_adv[adv] = live_by_adv.get(adv, 0) + 1
        clicks_by_adv[adv] = clicks_by_adv.get(adv, 0) + int(clicks or 0)
    launches_by_adv: dict[str, int] = {}
    for adv, in (db.query(models.LaunchLog.advertiser_id)
                 .filter(models.LaunchLog.ok == True, models.LaunchLog.created_at >= s_naive,     # noqa: E712
                         models.LaunchLog.created_at < end_utc.replace(tzinfo=None))):
        launches_by_adv[adv] = launches_by_adv.get(adv, 0) + 1
    total = pnl_data.overall_totals(db, start_utc, end_utc)
    prior_total = pnl_data.overall_totals(db, p_start, p_end)
    rows = []
    for u in people:
        ids = {aid for aid, uid in owner_of.items() if uid == u.id}
        k = pnl_data.overall_totals(db, start_utc, end_utc, ids)
        pk = pnl_data.overall_totals(db, p_start, p_end, ids)
        tt_clicks = sum(clicks_by_adv.get(a, 0) for a in ids) if range_key == "today" else 0
        rows.append({"user": u, "accounts": len(ids), "live": sum(live_by_adv.get(a, 0) for a in ids),
                     "spend": k["spend"], "revenue": k["revenue"], "profit": k["profit"], "roas": k["roas"],
                     "conversions": k["conversions"], "cpa": k["cpa"],
                     "epc": (k["revenue"] / tt_clicks) if tt_clicks else None,
                     "d_profit": k["profit"] - pk["profit"], "d_spend": _pct(k["spend"], pk["spend"]),
                     "has_prior": bool(pk["spend"] or pk["revenue"]),
                     "share": (k["spend"] / total["spend"] * 100) if total["spend"] else 0.0,
                     "launches": sum(launches_by_adv.get(a, 0) for a in ids),
                     "is_me": u.id == me.id})
    rows.sort(key=lambda r: r["profit"], reverse=True)
    unowned = sum(1 for uid in owner_of.values() if uid is None)
    labels = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days", "30d": "Last 30 days", "mtd": "Month to date", "custom": "Custom range"}
    qs = f"range={range_key}" + (f"&start={request.query_params.get('start')}&end={request.query_params.get('end')}" if range_key == "custom" else "")
    return render(request, "team.html", {
        "title": "Team", "rows": rows, "range_key": range_key, "range_label": labels[range_key], "unowned": unowned,
        "total": total, "prior_total": prior_total, "d_total_profit": total["profit"] - prior_total["profit"],
        "has_prior": bool(prior_total["spend"] or prior_total["revenue"]), "qs": qs, "n_days": max(1, int(round(length.total_seconds() / 86400))),
        "max_spend": max([r["spend"] for r in rows] + [0.0]),
    })


@router.get("/team/{user_id}/detail.json")
def team_detail(request: Request, user_id: int, db=Depends(get_db)):
    """One user's breakdown for the range: top sources and accounts by profit."""
    me = getattr(request.state, "user", None)
    if not users.is_owner(me):
        return JSONResponse({"ok": False, "error": "super admin only"}, status_code=403)
    u = db.get(models.User, user_id)
    if u is None:
        return JSONResponse({"ok": False, "error": "no such user"}, status_code=404)
    _range_key, start_utc, end_utc = _range(request)
    from .pnl_page import _slices
    sc = scope_mod.Scope(mode="user", ids=scope_mod.owned_ids(db, u.id), user_id=u.id)
    sl = _slices(db, start_utc, end_utc, sc)
    def pack(r):
        return {"name": r["name"], "sub": r.get("sub", ""), "spend": round(r["spend"], 2), "revenue": round(r["revenue"], 2),
                "profit": round(r["profit"], 2), "roas": round(r["roas"], 2), "conversions": int(r.get("conversions") or 0),
                "shared": bool(r.get("shared")), "href": r.get("href", "")}
    return JSONResponse({"ok": True, "user": u.email,
                         "sources": [pack(r) for r in sl["source"][:6]],
                         "accounts": [pack(r) for r in sl["account"][:6]],
                         "creatives": [pack(r) for r in sl["creative"][:4]],
                         "n_sources": len(sl["source"]), "n_accounts": len(sl["account"])})
