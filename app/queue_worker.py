"""Launch queue: items enqueue instantly, the background worker processes a few
per sweep, and TRANSIENT TikTok errors (rate limit, internal error, network)
retry up to `launch_retry_max`. Permanent errors mark the item failed and count
against the account's lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import error_messages, models, rules
from .settings_store import get_settings

TRANSIENT_CODES = {"40100", "50000", "HTTP", "APP"}


def enqueue(db: Session, template_id: int, spark_code_id: int | None,
            advertiser_ids: list[str] | None = None, auto_count: int = 0) -> str:
    """Create queue items — explicit accounts, or `auto_count` auto-pick slots
    (account chosen at process time so freshly-freed accounts qualify)."""
    batch_ref = error_messages.new_ref()
    if advertiser_ids:
        for adv in advertiser_ids:
            db.add(models.LaunchQueueItem(template_id=template_id,
                                          spark_code_id=spark_code_id,
                                          advertiser_id=adv, batch_ref=batch_ref))
    else:
        for _ in range(max(auto_count, 0)):
            db.add(models.LaunchQueueItem(template_id=template_id,
                                          spark_code_id=spark_code_id,
                                          advertiser_id="", batch_ref=batch_ref))
    db.commit()
    return batch_ref


def process(db: Session, settings: dict | None = None) -> int:
    """Run up to queue_per_sweep pending items. Returns how many were attempted."""
    from .routes import campaigns as engine
    from .routes import launch as launch_mod
    from .routes.super_launcher import eligible_accounts

    settings = settings or get_settings(db)
    limit = max(int(settings.get("queue_per_sweep") or 3), 1)
    retry_max = max(int(settings.get("launch_retry_max") or 3), 1)

    items = (db.query(models.LaunchQueueItem)
             .filter(models.LaunchQueueItem.status == "pending")
             .order_by(models.LaunchQueueItem.created_at)
             .limit(limit).all())
    if not items:
        return 0

    used_this_pass: set[str] = set()
    for item in items:
        item.status = "running"
        db.commit()
        template = db.get(models.Template, item.template_id)
        if not template:
            item.status = "failed"
            item.last_error = "preset no longer exists"
            db.commit()
            continue

        overrides = {}
        if item.spark_code_id:
            overrides["spark_code_id"] = item.spark_code_id
        fields = launch_mod.synthesize(template, overrides)

        # resolve target account
        acct = None
        if item.advertiser_id:
            acct = (db.query(models.AdAccount)
                    .filter_by(advertiser_id=item.advertiser_id).first())
        else:
            for cand in eligible_accounts(db, fields.get("account_policy", "new_only"),
                                          limit=len(used_this_pass) + 1):
                if cand.advertiser_id not in used_this_pass:
                    acct = cand
                    break
        if not acct:
            item.attempts += 1
            item.last_error = "no eligible account available"
            item.status = "failed" if item.attempts >= retry_max else "pending"
            db.commit()
            continue
        used_this_pass.add(acct.advertiser_id)

        log = engine.launch_to_account(db, acct, fields, item.batch_ref or "queue")
        rules.record_launch_outcome(db, acct, log.ok, settings)
        item.attempts += 1
        item.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if log.ok:
            item.status = "done"
            item.last_error = ""
            if not item.advertiser_id:
                item.advertiser_id = acct.advertiser_id  # record who got it
        else:
            item.last_error = f"[{log.error_code}] {log.error_message}"[:500]
            transient = str(log.error_code) in TRANSIENT_CODES
            if transient and item.attempts < retry_max:
                item.status = "pending"        # retried next sweep
            else:
                item.status = "failed"
        db.commit()
    return len(items)
