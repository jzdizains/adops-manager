"""Background worker with two cadences, tunable live from /settings:

  FAST sweep (default every 60s): sync metrics for accounts that have ACTIVE
    campaigns, take spend snapshots, evaluate auto-pause rules.
  SLOW cycle (every Nth fast sweep, default 5): full campaign sync for all
    accounts, BC + account balances, low-balance alerts, auto top-ups.

The split keeps 1-minute freshness where money moves without hammering
TikTok's rate limits across hundreds of idle accounts.

Disable entirely with env ADOPS_DISABLE_BG=1 (tests do this)."""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("adops.background")

MEM_WATCH_MB = 360      # a slow sweep whose RSS tops this writes one "mem" line to Diagnostics (512 MB box)
_started = False
_lock = threading.Lock()


def _posters_pass(db, limit: int = 3) -> int:
    """Generate posters for up to `limit` video creatives that don't have one yet,
    so the Creatives grid and the picker never trigger ffmpeg under page load."""
    from pathlib import Path
    from . import models
    from .routes import creatives as _cr
    n = 0
    try:
        for row in (db.query(models.Creative).filter(models.Creative.kind == "video", models.Creative.status != "processing",
                                                     models.Creative.file_path != "").order_by(models.Creative.id.desc()).limit(400)):
            out = _cr.THUMB_DIR / f"v{row.id}_{(row.md5 or 'x')[:12]}.jpg"
            if out.exists() or not Path(row.file_path).exists():
                continue
            _cr.ensure_poster(row)
            n += 1
            if n >= limit:
                break
    except Exception:  # noqa: BLE001
        log.exception("poster pass failed")
    return n


def _accounts_with_active_campaigns(db):
    from datetime import datetime, timedelta, timezone

    from . import models
    active_ids = {r[0] for r in
                  (db.query(models.CampaignRecord.advertiser_id)
                   .filter(models.CampaignRecord.operation_status == "ENABLE")
                   .distinct().all())}
    # accounts launched to in the last 24h sync fast too, even before the
    # first full sync picks their new campaigns up
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    active_ids |= {r[0] for r in
                   (db.query(models.LaunchLog.advertiser_id)
                    .filter(models.LaunchLog.ok == True,          # noqa: E712
                            models.LaunchLog.created_at >= cutoff)
                    .distinct().all())}
    if not active_ids:
        return []
    return (db.query(models.AdAccount)
            .filter(models.AdAccount.advertiser_id.in_(list(active_ids)),
                    models.AdAccount.enabled == True).all())  # noqa: E712


def rss_mb() -> float:
    """Current resident memory in MB (Linux; 0.0 elsewhere). No psutil needed."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0


def _loop():
    import gc

    from . import balances, bid_bump, issues, jobs, live_spend, partners, queue_worker, rules, tensorpix_worker
    from .database import SessionLocal
    from . import settings_store
    from .settings_store import get_settings

    time.sleep(20)  # let the app boot
    sweep_n = 0
    while True:
        db = SessionLocal()
        interval = 60
        try:
            settings = get_settings(db)
            interval = max(int(settings["sweep_interval_sec"]), 30)
            slow_every = max(int(settings["slow_every_n_sweeps"]), 1)
            sweep_n += 1
            slow = (sweep_n % slow_every == 0) or sweep_n == 1

            # memory breadcrumb: measure RSS after each step and remember the highest.
            # On a 512 MB box the OOM killer strikes on a transient spike the 1-minute
            # metric graph never samples, so the app records for itself which step the
            # RSS peaked at — but only writes a line when the peak is genuinely high, so
            # a healthy sweep costs nothing. `mem_watch` is read from Diagnostics.
            peak = {"mb": 0.0, "step": "start"}
            def _m(step):
                mb = rss_mb()
                if mb > peak["mb"]:
                    peak["mb"], peak["step"] = mb, step
                return mb
            _m("start")

            if slow:
                # full pass: every account, balances, alerts, top-ups, inventory
                balances.resync_structure(db); _m("resync_structure")   # BC list + account mapping + access-lost
                live_spend.sync_campaigns(db); _m("sync_campaigns")
                balances.sync_bc_balances(db); balances.sync_account_balances(db); _m("balances")
                balances.evaluate_bc_alerts(db)
                for u, us, ids in settings_store.per_user(db):     # each user's thresholds over their own accounts
                    rules.evaluate_topups(db, us, ids)
                    rules.check_fresh_inventory(db, us, u.id)
                    rules.check_pool_inventory(db, us, u.id)
                issues.scan(db); _m("issues.scan")
                partners.poll(db)               # TikTok-account assignments waiting on accepted invites
                jobs.prune(db)
                bid_bump.prune(db)
                _prune_logins(db)
                _audience_daily(db); _m("audience_daily")             # once a day: audience breakdowns + hourly heatmap
                _music_monthly(db); _m("music_monthly")               # TikTok's Audio Library cache, refreshed monthly (doc's advice)
            _audience_quick(db, settings); _m("audience_quick")       # every N minutes: today's hours / today+yesterday breakdowns, active accounts
            if not slow:
                # fast pass: only accounts with something running
                hot = _accounts_with_active_campaigns(db)
                if hot:
                    live_spend.sync_campaigns(db, hot); _m("sync_campaigns(hot)")
            for u, us, ids in settings_store.per_user(db):         # each user's rules over their own accounts
                rules.evaluate_pause_rules(db, us, ids)
                rules.evaluate_profit_rules(db, us, ids)
                bid_bump.schedule(db, us, ids, u.id)                  # idle ad groups → bid +step (runs as a job)
            queue_worker.process(db, settings); _m("queue_worker")
            tensorpix_worker.process_pending(db, limit=6); _m("tensorpix")   # advance variant jobs
            try:
                from .routes.creatives import recover_stuck_ai
                recover_stuck_ai(db, max_age_min=20)          # an AI edit that never came back (thread died) → failed + Retry
            except Exception:  # noqa: BLE001
                pass
            _posters_pass(db); _m("posters")                  # pre-make a few missing video posters, one at a time
            log.info("sweep %s done (slow=%s) rss=%.0fMB peak=%.0fMB@%s", sweep_n, slow, rss_mb(), peak["mb"], peak["step"])
            if peak["mb"] >= MEM_WATCH_MB:
                try:
                    from . import queries
                    queries.log(db, f"sweep {sweep_n} ({'slow' if slow else 'fast'}) memory peaked at {peak['mb']:.0f} MB during {peak['step']} (limit 512)", level="warning", source="mem")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # one bad sweep must never kill the worker
            log.exception("background sweep failed")
        finally:
            db.close()
        gc.collect()   # release sweep garbage promptly — RSS must not ratchet
        time.sleep(interval)


def start():
    global _started
    if os.environ.get("ADOPS_DISABLE_BG") == "1":
        return
    with _lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_loop, name="adops-background", daemon=True)
    t.start()


def _audience_quick(db, settings: dict) -> None:
    """Between the daily full pulls, keep the Audience page fresh for accounts
    with active campaigns: today's hour-by-hour delivery every
    audience_hours_every_min (near real-time), and today+yesterday breakdowns
    every audience_breakdown_every_min (TikTok publishes those 10–12 h late,
    so this mostly catches up late rows). Skipped while a refresh is already
    queued or running."""
    from datetime import date, datetime, timedelta
    from . import jobs, queries, timeutil
    if not queries.any_access_token(db) or jobs.pending(db, "audience_sync"):
        return
    now = timeutil.now_utc().replace(tzinfo=None)

    def due(key: str, every_min: int) -> bool:
        raw = queries.get_setting(db, key, "")
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return True
        return (now - last).total_seconds() >= every_min * 60

    today = date.fromisoformat(timeutil.local_date_str())
    y1 = (today - timedelta(days=1)).isoformat()
    hours_due = due("audience_hours_synced_at", int(settings.get("audience_hours_every_min") or 10))
    bd_due = due("audience_breakdown_synced_at", int(settings.get("audience_breakdown_every_min") or 60))
    if not hours_due and not bd_due:
        return
    days = {"hours": [today.isoformat()] if hours_due else [], "audience": [today.isoformat(), y1] if bd_due else []}
    title = ("Refresh audience breakdowns (today + yesterday, active accounts)" if bd_due
             else "Refresh today's hour-by-hour delivery (active accounts)")
    jobs.enqueue_once(db, "audience_sync", title, {"days": days, "hot_only": True}, href="/audience", quiet=True)


def _prune_logins(db) -> None:
    from . import auth_security
    try:
        auth_security.prune_attempts(db)
    except Exception:  # noqa: BLE001
        db.rollback()


def _music_monthly(db) -> None:
    """Queue the Audio Library sync when it has never run or is >30 days old."""
    from . import jobs, music_library, queries
    if not music_library.due(db) or not queries.any_access_token(db):
        return
    if jobs.pending(db, "music_sync"):
        return
    jobs.enqueue_once(db, "music_sync", "Refresh TikTok music library (monthly)", {}, href="/creatives?view=carousels", quiet=True)


def _audience_daily(db) -> None:
    """Queue the audience sync once per local day (slow lane; stoppable on
    the Jobs page). The store then serves the Audience page instantly."""
    from . import jobs, queries, timeutil
    today = timeutil.local_date_str()
    if queries.get_setting(db, "audience_sync_day", "") == today:
        return
    if not queries.any_access_token(db):
        return
    jobs.enqueue_once(db, "audience_sync", "Refresh audience breakdowns (daily)", {}, href="/audience", quiet=True)
    queries.set_setting(db, "audience_sync_day", today)
    db.commit()
