"""BC wallet + ad account balance sync, and the low-balance alert engine.

Alert policy (as specified): when a BC wallet drops below its threshold
($50 default), raise ONE in-app alert at the crossing, then remind at most
once every 24h while it stays low. Acknowledged alerts disappear from the
bell; the Overview banner shows any unacknowledged warn/err alerts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import config, models, queries, tiktok_api

REMIND_EVERY = timedelta(hours=24)


def bc_portal_url(bc_id: str) -> str:
    """Deep link to this BC in TikTok's Business Center portal (verified
    format: business.tiktok.com/manage/overview?org_id=<bc_id>)."""
    return f"https://business.tiktok.com/manage/overview?org_id={bc_id}"


def bc_threshold(bc: models.BusinessCenter) -> float:
    """Per-BC override, falling back to the BC_LOW_BALANCE_THRESHOLD env ($50)."""
    return float(bc.alert_threshold or config.BC_LOW_BALANCE_THRESHOLD or 50.0)


def sync_bc_balances(db: Session) -> int:
    """Refresh every BC wallet balance. Returns how many BCs were updated."""
    token = queries.any_access_token(db)
    if not token:
        return 0
    updated = 0
    now = datetime.now(timezone.utc)
    for bc in db.query(models.BusinessCenter).all():
        try:
            bal, cur = tiktok_api.parse_bc_balance(
                tiktok_api.get_bc_balance(token, bc.bc_id))
            bc.balance = bal
            if cur:
                bc.currency = cur
            bc.last_synced_at = now
            updated += 1
        except tiktok_api.TikTokError:
            continue
    db.commit()
    return updated


def sync_account_balances(db: Session) -> int:
    """Refresh ad-account balances per BC via /advertiser/balance/get/."""
    token = queries.any_access_token(db)
    if not token:
        return 0
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    updated = 0
    for bc in db.query(models.BusinessCenter).all():
        page = 1
        while True:
            try:
                data = tiktok_api.get_advertiser_balances(token, bc.bc_id, page=page)
            except tiktok_api.TikTokError:
                break
            for item in data.get("list", []):
                acct = accounts.get(str(item.get("advertiser_id", "")))
                if acct is not None:
                    try:
                        acct.balance = float(item.get("balance", item.get("cash_balance", 0)) or 0)
                        updated += 1
                    except (TypeError, ValueError):
                        pass
            total_pages = int((data.get("page_info", {}) or {}).get("total_page", 1) or 1)
            if page >= total_pages:
                break
            page += 1
    db.commit()
    return updated


def _latest_alert(db: Session, kind: str, ref_id: str) -> models.Alert | None:
    return (db.query(models.Alert).filter_by(kind=kind, ref_id=ref_id)
            .order_by(models.Alert.created_at.desc()).first())


def evaluate_bc_alerts(db: Session) -> list[models.Alert]:
    """Create low-balance alerts per the crossing/reminder policy."""
    created: list[models.Alert] = []
    now = datetime.now(timezone.utc)
    for bc in db.query(models.BusinessCenter).all():
        threshold = bc_threshold(bc)
        label = bc.name or bc.bc_id
        if bc.balance is not None and bc.balance < threshold:
            last = _latest_alert(db, "bc_low_balance", bc.bc_id)
            recent = False
            if last:
                last_at = last.created_at
                if last_at is not None and last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
                recent = last_at is not None and (now - last_at) < REMIND_EVERY
            if not recent:
                alert = models.Alert(
                    kind="bc_low_balance", ref_id=bc.bc_id, level="warn",
                    message=(f"Business Center “{label}” wallet is low: "
                             f"{bc.currency} {bc.balance:.2f} (threshold {threshold:.0f}). "
                             "Top it up to keep campaigns funded."))
                db.add(alert)
                created.append(alert)
    db.commit()
    return created


def unacknowledged(db: Session, limit: int = 50) -> list[models.Alert]:
    return (db.query(models.Alert).filter_by(acknowledged=False)
            .order_by(models.Alert.created_at.desc()).limit(limit).all())


def run_sweep(db: Session) -> dict:
    """One full pass: balances + alerts. Called by the background worker
    and by the manual 'Sync now' actions."""
    bcs = sync_bc_balances(db)
    accts = sync_account_balances(db)
    alerts = evaluate_bc_alerts(db)
    return {"bcs": bcs, "accounts": accts, "new_alerts": len(alerts)}
