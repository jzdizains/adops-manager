"""Times TikTok reads in the AD ACCOUNT's own timezone.

An ad group's `schedule_start_time` ("YYYY-MM-DD HH:MM:SS") is read in the ad account's
timezone (/advertiser/info `timezone`, e.g. "Etc/GMT+5" = UTC−5), not in UTC. Sending UTC
"now" made a UTC−5 account start 5 hours late, and a UTC+8 account's "now" lie 8 hours in
the past (refused or back-dated). "Start now" is therefore the account's own clock + 60 s.

Unknown or unreadable timezone → UTC (the old behaviour), never an error.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone, tzinfo

LEAD_S = 60
FMT = "%Y-%m-%d %H:%M:%S"
_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.I)


def zone(name: str | None) -> tzinfo | None:
    """IANA name ("America/New_York", "Etc/GMT+5") or an offset ("UTC+08:00", "GMT-5", "+0530")
    → tzinfo; None when empty or not understood. Pure."""
    name = (name or "").strip()
    if not name:
        return None
    if name.upper() in ("UTC", "GMT", "ETC/UTC", "ETC/GMT", "Z"):
        return timezone.utc
    m = _OFFSET.match(name.replace(" ", ""))
    if m and not name.lower().startswith("etc/"):
        sign = -1 if m.group(1) == "-" else 1
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h <= 14 and mi < 60:
            return timezone(sign * timedelta(hours=h, minutes=mi))
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:      # noqa: BLE001 — unknown key, missing tzdata, bad string
        return None


def tiktok_time(when: datetime, tz_name: str | None) -> str:
    """`when` (aware, or naive = UTC) as TikTok wants it for this account. Pure."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    z = zone(tz_name) or timezone.utc
    return when.astimezone(z).strftime(FMT)


def start_now(tz_name: str | None, lead_s: int = LEAD_S, now: datetime | None = None) -> str:
    """"Start now" for an ad group on this account: its own clock + `lead_s` seconds."""
    now = now or datetime.now(timezone.utc)
    return tiktok_time(now + timedelta(seconds=lead_s), tz_name)


def label(tz_name: str | None) -> str:
    """Short human label: "UTC−5", "UTC+8", "UTC" (unknown → "UTC (unknown)"). Pure."""
    z = zone(tz_name)
    if z is None:
        return "UTC (timezone unknown)"
    off = datetime.now(timezone.utc).astimezone(z).utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    if not mins:
        return "UTC"
    sign = "+" if mins > 0 else "−"
    h, m = divmod(abs(mins), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


_MISS: dict[str, float] = {}       # advertiser → when a lookup last failed (don't hammer TikTok)


def account_tz(db, acct) -> str:
    """The account's timezone name. Read from the row; when the sync never filled it, asked
    from TikTok once (/advertiser/info) and stored. "" when unknown."""
    tz = (getattr(acct, "timezone", "") or "").strip()
    if tz:
        return tz
    adv = str(getattr(acct, "advertiser_id", "") or "")
    if not adv or not getattr(acct, "access_token", "") or time.time() - _MISS.get(adv, 0) < 3600:
        return ""
    try:
        from . import tiktok_api
        info = tiktok_api.get_advertiser_info(acct.access_token, [adv]) or []
        row = info[0] if info else {}
        tz = str(row.get("timezone") or row.get("display_timezone") or "").strip()
    except Exception:      # noqa: BLE001 — a timezone lookup never blocks a launch
        tz = ""
    if not tz:
        _MISS[adv] = time.time()
        return ""
    try:
        acct.timezone = tz
        db.commit()
    except Exception:      # noqa: BLE001
        db.rollback()
    return tz
