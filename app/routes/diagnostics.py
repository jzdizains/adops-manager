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
    by_token = {}
    try:
        users = {u.id: u.email for u in db.query(models.User)}
        by_token = {lg.access_token: (users.get(lg.user_id) or f"user {lg.user_id}") for lg in db.query(models.TikTokLogin)}
    except Exception:  # noqa: BLE001
        db.rollback()
    out = []
    for g in groups[:25]:
        g["user"] = by_token.get(g["token"], "")
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
        out.append({"key": g["key"], "n": len(g["accounts"]), "accounts": sorted(g["accounts"])[:400], "user": g.get("user", ""), **info, "error": err})
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


# ---- capacity (v155.30) ------------------------------------------------------------------------
BIG_TABLES = ("ad_accounts", "campaign_records", "spend_snapshots", "launch_logs", "launch_traces", "adgroup_snapshots",
              "adgroup_states", "alerts", "clicks", "postback_events", "lander_events", "hourly_metrics", "audience_stats",
              "diag_events", "app_logs", "jobs", "audit_logs", "activity_events", "rule_actions", "creatives", "creative_uploads")


def _dir_size(path, cap_files: int = 40000) -> tuple[int, int]:
    """(bytes, files) under `path`, at most cap_files entries walked."""
    import os
    total = n = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.stat(os.path.join(root, f)).st_size
                    n += 1
                except OSError:
                    continue
                if n >= cap_files:
                    return total, n
    except OSError:
        pass
    return total, n


def capacity_report(db) -> dict:
    """What the dashboard holds and how much room is left: database + WAL size, disk, the biggest
    tables' row counts, memory against the limit, the sweep's timing and the retention windows."""
    import os
    import shutil
    from sqlalchemy import text
    from .. import background, retention, sched
    dbp = config.DB_PATH
    size = {"db_mb": 0.0, "wal_mb": 0.0}
    try:
        size["db_mb"] = round(os.stat(dbp).st_size / 1e6, 1)
        size["wal_mb"] = round(os.stat(str(dbp) + "-wal").st_size / 1e6, 1) if os.path.exists(str(dbp) + "-wal") else 0.0
    except OSError:
        pass
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        disk = {"total_gb": round(du.total / 1e9, 2), "free_gb": round(du.free / 1e9, 2), "used_pct": round(100 * (du.total - du.free) / du.total, 1) if du.total else 0}
    except OSError:
        disk = {}
    media_b, media_n = _dir_size(config.DATA_DIR / "creatives")
    thumbs_b, thumbs_n = _dir_size(config.DATA_DIR / "thumbs")
    counts = {}
    for t in BIG_TABLES:
        try:
            counts[t] = int(db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
        except Exception:  # noqa: BLE001 — a table that doesn't exist on this build
            db.rollback()
    accounts = {"total": counts.get("ad_accounts", 0),
                "enabled": int(db.query(models.AdAccount).filter(models.AdAccount.enabled == True).count()),        # noqa: E712
                "lost": int(db.query(models.AdAccount).filter(models.AdAccount.status == "ACCESS_LOST").count()),
                "logins": len({r[0] for r in db.query(models.AdAccount.access_token).filter(models.AdAccount.access_token != "")})}
    sw = dict(sched.SWEEP)
    from .. import queries
    import json as _json
    try:
        rep = _json.loads(queries.get_setting(db, "campaign_sync_report", "") or "{}")
    except ValueError:
        rep = {}
    return {"ok": True, "size": size, "disk": disk, "media": {"mb": round(media_b / 1e6, 1), "files": media_n, "thumbs_mb": round(thumbs_b / 1e6, 1), "thumbs": thumbs_n},
            "tables": counts, "accounts": accounts,
            "memory": {"rss_mb": round(background.rss_mb()), "limit_mb": background.mem_limit_mb(), "shed_mb": background.MEM_SHED_MB},
            "sweep": {"n": sw.get("n"), "last_s": sw.get("dur"), "slow": sw.get("slow"), "interval": sw.get("interval"),
                      "full_batch": background.FULL_SYNC_MAX, "full_budget_s": background.FULL_SYNC_BUDGET_S,
                      "last_sync": {"synced": rep.get("synced"), "left": rep.get("left"), "errors": len(rep.get("errors") or []), "at": rep.get("at")}},
            "retention": retention.plan()}


@router.get("/diagnostics/capacity.json")
def capacity_json(request: Request, db: Session = Depends(get_db)):
    """Owner only; read-only. A few COUNT(*) queries and a walk of the media folder."""
    if not guard.is_owner(request):
        return JSONResponse({"ok": False, "error": guard.OWNER_ONLY_MSG}, status_code=403)
    return capacity_report(db)


# ---- uptime & incidents (v155.36) ----------------------------------------------------------------
_PROCESS_STARTED = __import__("time").time()
UPTIME_HOURS = 72


def uptime_report(db, hours: int = UPTIME_HOURS) -> dict:
    """The last `hours` of the app log that explain an outage: every (re)start ("boot"), the
    memory breadcrumbs ("mem"), request crashes (500s), and the sweep's own failures — as one
    timeline, newest first, plus the process uptime and the last sweep. A boot line with nothing
    but memory lines before it is the signature of an out-of-memory kill."""
    import time as _time
    from datetime import datetime, timedelta
    from .. import background, sched
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (db.query(models.AppLog).filter(models.AppLog.created_at >= since)
            .filter((models.AppLog.source.in_(("boot", "mem", "sweep", "http"))) | (models.AppLog.level.in_(("error", "critical"))))
            .order_by(models.AppLog.id.desc()).limit(400).all())
    lines = [{"at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "", "level": r.level or "", "source": r.source or "",
              "message": (r.message or "")[:400]} for r in rows]
    boots = [x for x in lines if x["source"] == "boot"]
    # for each boot, what the log said just before it (the last few lines of the previous life)
    incidents = []
    for i, b in enumerate(boots):
        before = [x for x in lines if x["at"] < b["at"]][:6]
        incidents.append({"boot": b, "before": before,
                          "looks_like": ("out of memory (memory breadcrumbs right before the restart)" if any(x["source"] == "mem" for x in before[:3])
                                         else ("a deploy / manual restart" if not before else "a crash or restart — see the lines before"))})
    return {"ok": True, "hours": hours, "uptime_s": int(_time.time() - _PROCESS_STARTED), "build": config.build_id(),
            "memory": {"rss_mb": round(background.rss_mb()), "limit_mb": background.mem_limit_mb()},
            "sweep": sched.snapshot()["sweep"], "boots": len(boots), "incidents": incidents, "lines": lines[:150]}


@router.get("/diagnostics/uptime.json")
def uptime_json(request: Request, db: Session = Depends(get_db)):
    if not guard.is_owner(request):
        return JSONResponse({"ok": False, "error": guard.OWNER_ONLY_MSG}, status_code=403)
    return uptime_report(db)
