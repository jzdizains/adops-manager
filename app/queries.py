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
    row = db.query(models.Setting).filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))
    db.commit()


def enabled_accounts(db: Session) -> list[models.AdAccount]:
    return (db.query(models.AdAccount)
            .filter(models.AdAccount.enabled == True)  # noqa: E712
            .order_by(models.AdAccount.advertiser_name).all())


def any_access_token(db: Session) -> str:
    acct = db.query(models.AdAccount).filter(models.AdAccount.access_token != "").first()
    return acct.access_token if acct else ""


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
