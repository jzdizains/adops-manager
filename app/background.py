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

_started = False
_lock = threading.Lock()


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

    from . import balances, issues, jobs, live_spend, partners, queue_worker, rules, tensorpix_worker
    from .database import SessionLocal
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

            if slow:
                # full pass: every account, balances, alerts, top-ups, inventory
                balances.resync_structure(db)   # BC list + account mapping + access-lost
                live_spend.sync_campaigns(db)
                balances.sync_bc_balances(db)
                balances.sync_account_balances(db)
                balances.evaluate_bc_alerts(db)
                rules.evaluate_topups(db, settings)
                rules.check_fresh_inventory(db, settings)
                rules.check_pool_inventory(db, settings)
                issues.scan(db)
                partners.poll(db)               # TikTok-account assignments waiting on accepted invites
                jobs.prune(db)
                _audience_daily(db)             # once a day: audience breakdowns + hourly heatmap
            _audience_quick(db, settings)       # every N minutes: today's hours / today+yesterday breakdowns, active accounts
            if not slow:
                # fast pass: only accounts with something running
                hot = _accounts_with_active_campaigns(db)
                if hot:
                    live_spend.sync_campaigns(db, hot)
            rules.evaluate_pause_rules(db, settings)
            rules.evaluate_profit_rules(db, settings)
            queue_worker.process(db, settings)
            tensorpix_worker.process_pending(db, limit=6)   # advance variant jobs
            log.info("sweep %s done (slow=%s) rss=%.0fMB", sweep_n, slow, rss_mb())
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
    jobs.enqueue_once(db, "audience_sync", title, {"days": days, "hot_only": True}, href="/audience")


def _audience_daily(db) -> None:
    """Queue the audience sync once per local day (slow lane; stoppable on
    the Jobs page). The store then serves the Audience page instantly."""
    from . import jobs, queries, timeutil
    today = timeutil.local_date_str()
    if queries.get_setting(db, "audience_sync_day", "") == today:
        return
    if not queries.any_access_token(db):
        return
    jobs.enqueue_once(db, "audience_sync", "Refresh audience breakdowns (daily)", {}, href="/audience")
    queries.set_setting(db, "audience_sync_day", today)
    db.commit()
