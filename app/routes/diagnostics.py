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

from .. import config, diag, models
from ..database import get_db
from ..templating import render
from . import guard

router = APIRouter()

KINDS = (("", "Everything"), ("tiktok", "TikTok"), ("job", "Background jobs"), ("app", "App"))


@router.get("/diagnostics")
def diagnostics_page(request: Request, db: Session = Depends(get_db)):
    # the feed holds every workspace's errors (endpoints, advertiser ids, request bodies)
    if not guard.is_owner(request):
        return RedirectResponse("/settings?err=" + quote(guard.OWNER_ONLY_MSG), status_code=303)
    kind = request.query_params.get("kind", "")
    q = request.query_params.get("q", "")
    rows = diag.recent(db, limit=200, kind=kind, q=q)
    from .. import jobs, sched
    lanes = {}
    for j in db.query(models.Job).filter(models.Job.status.in_(("queued", "claimed", "running"))):
        ln = jobs.lane(j.kind)
        lanes.setdefault(ln, {"queued": 0, "running": 0})
        lanes[ln]["running" if j.status in ("claimed", "running") else "queued"] += 1
    from .. import migrations
    from ..database import engine
    try:
        migs = migrations.applied(engine)
    except Exception:      # noqa: BLE001
        migs = []
    mock = None
    if config.MOCK_TIKTOK:
        from .. import tiktok_mock
        mock = tiktok_mock.status()
    return render(request, "diagnostics.html", {
        "migrations": migs, "migrations_total": len(migrations.STEPS), "mock": mock,
        "sched": sched.snapshot(), "lanes": lanes,
        "title": "Diagnostics", "active": "settings",
        "rows": [diag.as_json(r) for r in rows],
        "kind": kind, "q": q, "kinds": KINDS, "unseen": diag.unseen_count(db),
    })


_WHO: dict = {}          # sha256(token)[:16] → (expires, info) — who a connection belongs to
_WHO_TTL = 600


def group_connections(accounts) -> list[dict]:
    """Ad accounts grouped by the TikTok connection (access token) they use. Pure; the token itself
    never leaves this function — only a short fingerprint does."""
    import hashlib
    out: dict = {}
    for a in accounts:
        tok = a.access_token or ""
        if not tok:
            continue
        key = hashlib.sha256(tok.encode()).hexdigest()[:16]
        g = out.setdefault(key, {"key": key, "token": tok, "accounts": []})
        g["accounts"].append(a.advertiser_name or a.advertiser_id)
    return sorted(out.values(), key=lambda g: -len(g["accounts"]))


@router.get("/diagnostics/connections.json")
def connections_json(request: Request, db: Session = Depends(get_db)):
    """Who connected TikTok (v155.12): per connection, the TikTok for Business login it belongs to
    and the ad accounts using it. Owner only; one read-only /user/info/ call per connection, cached."""
    import time as _time
    if not guard.is_owner(request):
        return JSONResponse({"ok": False, "error": guard.OWNER_ONLY_MSG}, status_code=403)
    from .. import tiktok_api
    groups = group_connections(db.query(models.AdAccount).filter(models.AdAccount.enabled == True).all())   # noqa: E712
    db.rollback()                      # no DB connection held while TikTok answers
    out = []
    for g in groups[:25]:
        hit = _WHO.get(g["key"])
        if hit and hit[0] > _time.time():
            info, err = hit[1], ""
        else:
            try:
                d = tiktok_api.user_info(g["token"])
                info, err = {"name": d.get("display_name") or "", "email": d.get("email") or "", "id": str(d.get("core_user_id") or "")}, ""
                _WHO[g["key"]] = (_time.time() + _WHO_TTL, info)
            except tiktok_api.TikTokError as e:
                info, err = {}, f"{e.message} (code {e.code})"
        out.append({"key": g["key"], "n": len(g["accounts"]), "accounts": sorted(g["accounts"])[:400], **info, "error": err})
    return JSONResponse({"ok": True, "connections": out, "more": max(0, len(groups) - 25)})


@router.post("/diagnostics/mock/{action}")
def mock_control(action: str, request: Request):
    """TikTok test mode: switch the simulated outage, or forget every simulated campaign.
    Only when test mode is on (local development) — 404 anywhere else."""
    if not config.MOCK_TIKTOK or not guard.is_owner(request):
        return JSONResponse({"ok": False, "error": "not available"}, status_code=404)
    from .. import tiktok_mock
    if action == "outage":
        on = tiktok_mock.set_outage(not tiktok_mock.status()["outage"])
        return JSONResponse({"ok": True, "outage": on})
    if action == "reset":
        tiktok_mock.reset()
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)


@router.get("/diagnostics.json")
def diagnostics_json(request: Request, db: Session = Depends(get_db)):
    """The same feed, machine-readable — so the errors can be read and acted on
    directly rather than retyped out of a screenshot."""
    if not guard.is_owner(request):
        return JSONResponse({"ok": False, "error": guard.OWNER_ONLY_MSG}, status_code=403)
    kind = request.query_params.get("kind", "")
    q = request.query_params.get("q", "")
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    rows = diag.recent(db, limit=limit, kind=kind, q=q)
    return JSONResponse({"ok": True, "count": len(rows), "build": config.build_id(),
                         "unseen": diag.unseen_count(db),
                         "events": [diag.as_json(r) for r in rows]})


@router.post("/diagnostics/seen")
def diagnostics_seen(request: Request, db: Session = Depends(get_db)):
    if not guard.is_owner(request):
        return RedirectResponse("/settings?err=" + quote(guard.OWNER_ONLY_MSG), status_code=303)
    diag.mark_seen(db)
    return RedirectResponse("/diagnostics?" + quote("ok=1"), status_code=303)
