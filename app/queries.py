"""Reporting helpers shared by dashboard / status / performance pages."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, timeutil


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(models.Setting).filter_by(key=key).first()
    return row.value if row else default


def set_setting(db: Session, key: str, value: str):
    """Write one key. Atomic upsert — two users (or a user and the sweep) writing the
    same key at the same moment must never race on the INSERT (UNIQUE key)."""
    upsert_setting(db, key, value)
    db.commit()


def insert_setting_if_absent(db: Session, key: str, value: str) -> None:
    """INSERT … ON CONFLICT DO NOTHING — for first-time defaults minted by several
    simultaneous readers: exactly one value lands and everybody re-reads that one."""
    from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        stmt = _sqlite_insert(models.Setting).values(key=key, value=value).on_conflict_do_nothing(index_elements=["key"])
        db.execute(stmt)
        return
    if not db.query(models.Setting).filter_by(key=key).first():
        db.add(models.Setting(key=key, value=value))


def upsert_setting(db: Session, key: str, value: str) -> None:
    """INSERT … ON CONFLICT(key) DO UPDATE (no read-then-insert window). Does not commit.
    Any Setting object for this key already loaded in the session is refreshed."""
    from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        stmt = _sqlite_insert(models.Setting).values(key=key, value=value, updated_at=now)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now})
        db.execute(stmt)
        for obj in list(db.identity_map.values()):
            if isinstance(obj, models.Setting) and obj.key == key:
                db.expire(obj)
        return
    row = db.query(models.Setting).filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))


def enabled_accounts(db: Session) -> list[models.AdAccount]:
    return (db.query(models.AdAccount)
            .filter(models.AdAccount.enabled == True)  # noqa: E712
            .order_by(models.AdAccount.advertiser_name).all())


def any_access_token(db: Session) -> str:
    acct = db.query(models.AdAccount).filter(models.AdAccount.access_token != "").first()
    return acct.access_token if acct else ""


def token_for_bc(db: Session, bc_id: str) -> str:
    """The TikTok login that can see this Business Center (v116 — several logins):
    the token stamped on the BC when its login listed it, else any account under it,
    else any token at all (single-login installs, exactly as before)."""
    bc = db.query(models.BusinessCenter).filter_by(bc_id=str(bc_id or "")).first() if bc_id else None
    if bc is not None and bc.access_token:
        return bc.access_token
    if bc_id:
        acct = (db.query(models.AdAccount)
                .filter(models.AdAccount.owner_bc_id == str(bc_id), models.AdAccount.access_token != "").first())
        if acct:
            return acct.access_token
    return any_access_token(db)


def token_for_user(db: Session, user_id) -> str:
    """A token from the user's own workspace (any of their accounts / BCs); falls back
    to any token when they have none yet (or user_id is None — the whole company)."""
    if user_id is not None:
        acct = (db.query(models.AdAccount)
                .filter(models.AdAccount.owner_user_id == int(user_id), models.AdAccount.access_token != "").first())
        if acct:
            return acct.access_token
        try:                                          # v155.38: a user whose login only lists others' accounts
            acc = db.query(models.AccountAccess).filter(models.AccountAccess.user_id == int(user_id), models.AccountAccess.access_token != "").first()
            if acc:
                return acc.access_token
        except Exception:  # noqa: BLE001
            db.rollback()
        bc = (db.query(models.BusinessCenter)
              .filter(models.BusinessCenter.owner_user_id == int(user_id), models.BusinessCenter.access_token != "").first())
        if bc:
            return bc.access_token
    return any_access_token(db)


def distinct_tokens(db: Session) -> list[tuple[str, "models.AdAccount"]]:
    """One (token, sample account) per connected TikTok login."""
    seen: dict[str, models.AdAccount] = {}
    for a in db.query(models.AdAccount).filter(models.AdAccount.access_token != ""):
        seen.setdefault(a.access_token, a)
    return list(seen.items())


def revenue_between(db: Session, start_utc: datetime, end_utc: datetime) -> dict:
    """Real revenue from persisted ConversionSample rows (never live calls)."""
    q = (db.query(func.coalesce(func.sum(models.ConversionSample.revenue), 0.0),
                  func.coalesce(func.sum(models.ConversionSample.conversions), 0))
         .filter(models.ConversionSample.sampled_at >= start_utc.replace(tzinfo=None),
                 models.ConversionSample.sampled_at < end_utc.replace(tzinfo=None)))
    revenue, conversions = q.one()
    return {"revenue": float(revenue or 0), "conversions": int(conversions or 0)}


def spend_today(db: Session) -> float:
    total = (db.query(func.coalesce(func.sum(models.CampaignRecord.spend_today), 0.0))
             .scalar())
    return float(total or 0)


def campaigns_synced_ago(db: Session) -> str:
    latest = db.query(func.max(models.CampaignRecord.synced_at)).scalar()
    if not latest:
        return "never"
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    delta = timeutil.now_utc() - latest
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    return f"{mins // 60}h {mins % 60}m ago"


def log(db: Session, message: str, level: str = "info", source: str = ""):
    db.add(models.AppLog(level=level, source=source, message=message))
    db.commit()
