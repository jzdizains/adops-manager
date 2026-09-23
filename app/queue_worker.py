"""Launch queue: items enqueue instantly, the background worker processes a few
per sweep, and TRANSIENT TikTok errors (rate limit, internal error, network)
retry up to `launch_retry_max`. Permanent errors mark the item failed and count
against the account's lifecycle."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import error_messages, models, rules
from .settings_store import get_settings

TRANSIENT_CODES = {"40100", "50000", "HTTP", "APP"}
_RUN = threading.Lock()        # one pass at a time per process (the sweep and "Process now" never overlap)


def enqueue(db: Session, template_id: int, spark_code_id: int | None,
            advertiser_ids: list[str] | None = None, auto_count: int = 0,
            use_library: bool = False, launched_by: int | None = None, exclude: list[str] | None = None) -> str:
    """Create queue items — explicit accounts, or `auto_count` auto-pick slots
    (account chosen at process time so freshly-freed accounts qualify). `exclude`: accounts the
    Review step left out, never auto-picked for this batch."""
    batch_ref = error_messages.new_ref()
    if exclude and not advertiser_ids:
        from . import queries
        queries.set_setting(db, f"queue_exclude:{batch_ref}", json.dumps([str(x) for x in exclude][:500]))
    if advertiser_ids:
        for adv in advertiser_ids:
            db.add(models.LaunchQueueItem(template_id=template_id,
                                          spark_code_id=spark_code_id,
                                          use_library=use_library, launched_by=launched_by,
                                          advertiser_id=adv, batch_ref=batch_ref))
    else:
        for _ in range(max(auto_count, 0)):
            db.add(models.LaunchQueueItem(template_id=template_id,
                                          spark_code_id=spark_code_id,
                                          use_library=use_library, launched_by=launched_by,
                                          advertiser_id="", batch_ref=batch_ref))
    db.commit()
    return batch_ref


def process(db: Session, settings: dict | None = None) -> int:
    """Run up to queue_per_sweep pending items. Returns how many were attempted (0 when a pass
    is already running in this process)."""
    if not _RUN.acquire(blocking=False):
        return 0
    try:
        return _process(db, settings)
    finally:
        _RUN.release()


def _exclusions(db: Session, batch_ref: str, cache: dict) -> set:
    if batch_ref not in cache:
        from . import queries
        try:
            cache[batch_ref] = set(json.loads(queries.get_setting(db, f"queue_exclude:{batch_ref}", "") or "[]"))
        except (ValueError, TypeError):
            cache[batch_ref] = set()
    return cache[batch_ref]


def retry_safe(db: Session, item) -> bool:
    """May this failed queue item be re-queued? Not when its launch left a campaign behind (or a
    create may have landed unseen), and not when a restart cut it off with nothing proving it
    safe — re-queuing would put a second campaign on the account."""
    from .routes.campaigns import relaunch_safe
    log = None
    if item.batch_ref and item.advertiser_id:
        log = (db.query(models.LaunchLog).filter(models.LaunchLog.batch_ref == item.batch_ref,
                                                 models.LaunchLog.advertiser_id == item.advertiser_id)
               .order_by(models.LaunchLog.id.desc()).first())
    if log is not None:
        return relaunch_safe(log)
    return "interrupted by a restart" not in (item.last_error or "")


def _process(db: Session, settings: dict | None = None) -> int:
    settings = settings or get_settings(db)
    limit = max(int(settings.get("queue_per_sweep") or 3), 1)
    retry_max = max(int(settings.get("launch_retry_max") or 3), 1)

    pending = (db.query(models.LaunchQueueItem)
               .filter(models.LaunchQueueItem.status == "pending")
               .order_by(models.LaunchQueueItem.created_at)
               .limit(limit * 20).all())
    # fair across users (v116): one item per user per round, oldest first, so a buyer
    # who queued fifty launches never starves the one who queued two
    by_user: dict = {}
    for it in pending:
        by_user.setdefault(it.launched_by, []).append(it)
    items = []
    while len(items) < limit and any(by_user.values()):
        for uid in list(by_user):
            if by_user[uid]:
                items.append(by_user[uid].pop(0))
                if len(items) >= limit:
                    break
            else:
                by_user.pop(uid)
    if not items:
        return 0

    used_this_pass: set[str] = set()
    excl_cache: dict = {}
    for item in items:
        # claim it atomically: another process (or an older pass) may already have it
        claimed = (db.query(models.LaunchQueueItem)
                   .filter(models.LaunchQueueItem.id == item.id, models.LaunchQueueItem.status == "pending")
                   .update({models.LaunchQueueItem.status: "running"}, synchronize_session=False))
        db.commit()
        if not claimed:
            continue
        item.status = "running"
        try:
            _run_item(db, item, used_this_pass, excl_cache, retry_max)
        except Exception as e:      # noqa: BLE001 — never leave an item stuck on "running"
            db.rollback()
            try:
                it = db.get(models.LaunchQueueItem, item.id)
                if it is not None and it.status == "running":
                    it.status, it.last_error = "failed", f"queue error: {type(e).__name__}: {str(e)[:300]}"
                    db.commit()
            except Exception:      # noqa: BLE001
                db.rollback()
    return len(items)


def _run_item(db: Session, item, used_this_pass: set, excl_cache: dict, retry_max: int) -> None:
    from .routes import campaigns as engine
    from .routes import launch as launch_mod
    from .routes.super_launcher import eligible_accounts
    template = db.get(models.Template, item.template_id)
    if not template:
        item.status = "failed"
        item.last_error = "preset no longer exists"
        db.commit()
        return

    overrides: dict = {}
    if item.spark_code_id:
        overrides["spark_code_id"] = item.spark_code_id
        overrides["creative_source"] = "spark"
        overrides["ad_text_mode"] = "fixed"     # pool texts are library-only
    elif item.use_library:
        overrides["creative_source"] = "library"
    fields = launch_mod.synthesize(template, overrides)
    fields["_launched_by"] = item.launched_by or template.owner_user_id

    # resolve target account
    acct = None
    if item.advertiser_id:
        acct = (db.query(models.AdAccount)
                .filter_by(advertiser_id=item.advertiser_id).first())
    else:
        for cand in eligible_accounts(db, fields.get("account_policy", "new_only"),
                                      limit=len(used_this_pass) + 1,
                                      owner_user_id=(item.launched_by or template.owner_user_id),
                                      fields=fields, exclude=_exclusions(db, item.batch_ref or "", excl_cache)):
            if cand.advertiser_id not in used_this_pass:
                acct = cand
                break
    if not acct:
        item.attempts += 1
        item.last_error = "no eligible account available"
        item.status = "failed" if item.attempts >= retry_max else "pending"
        db.commit()
        return
    used_this_pass.add(acct.advertiser_id)

    log = engine.launch_to_account(db, acct, fields, item.batch_ref or "queue")
    if log.error_code not in ("ASSET", "CONFIG"):   # preset problems, not account health
        from . import settings_store
        rules.record_launch_outcome(db, acct, log.ok, settings_store.for_account(db, acct.advertiser_id))
    item.attempts += 1
    item.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if log.ok:
        item.status = "done"
        item.last_error = ""
        if not item.advertiser_id:
            item.advertiser_id = acct.advertiser_id  # record who got it
    else:
        item.last_error = f"[{log.error_code}] {log.error_message}"[:500]
        from . import tiktok_api as _api
        blob = f"{log.error_message} {log.error_technical}".lower()
        transient = (str(log.error_code) in TRANSIENT_CODES
                     or any(m in blob for m in _api._TRANSIENT_MSGS))
        # never re-run an account that already has live ads from this launch, or where a
        # create may have landed unseen — the retry would make a second campaign there
        if not engine.relaunch_safe(log):
            transient = False
        if transient and item.attempts < retry_max:
            item.status = "pending"        # retried next sweep
        else:
            item.status = "failed"
    db.commit()
