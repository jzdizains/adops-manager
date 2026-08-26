"""Business-timezone helpers. The app runs on ONE fixed business timezone."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

TZ = ZoneInfo(config.BUSINESS_TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    """Current time in the business timezone."""
    return datetime.now(TZ)


def local_midnight_utc(day_offset: int = 0) -> datetime:
    """UTC instant of local midnight (start of today + offset days)."""
    local = now_local() + timedelta(days=day_offset)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def local_date_str(dt: datetime | None = None) -> str:
    dt = dt or now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%Y-%m-%d")


def fmt_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime(fmt)


def range_bounds(range_key: str, start: str | None = None, end: str | None = None):
    """Resolve a date-range picker selection to (start_utc, end_utc).

    range_key: today | yesterday | 7d | 30d | mtd | custom
    """
    if range_key == "custom" and start and end:
        s = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=TZ)
        e = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=TZ) + timedelta(days=1)
        return s.astimezone(timezone.utc), e.astimezone(timezone.utc)
    if range_key == "yesterday":
        return local_midnight_utc(-1), local_midnight_utc(0)
    if range_key == "7d":
        return local_midnight_utc(-6), local_midnight_utc(1)
    if range_key == "30d":
        return local_midnight_utc(-29), local_midnight_utc(1)
    if range_key == "mtd":
        local = now_local().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return local.astimezone(timezone.utc), local_midnight_utc(1)
    # default: today
    return local_midnight_utc(0), local_midnight_utc(1)
