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

    from . import balances, issues, live_spend, partners, queue_worker, rules, tensorpix_worker
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
            else:
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
