"""When an ad group starts — and in whose clock.

v155.32 (25 Sep 2026, verified on a live ad group): TikTok's API reads an ad group's
`schedule_start_time` ("YYYY-MM-DD HH:MM:SS") in **UTC+0**, whatever the ad account's own
timezone. v151 had it the other way round — it sent the account's local clock — which on a
UTC+5 account (Asia/Karachi) put the start 5 hours into the future ("Scheduled" in Ads
Manager), on a European UTC+1 account 1 hour, and on a UTC−5 account 5 hours into the past
(so those started at once and nobody noticed). Ads Manager DISPLAYS the start in the
account's timezone, which is what made the UTC value look "5 hours late" back then.

"Start now" is therefore UTC now + 60 s. The account timezone is still kept for labels.
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
    """`when` (aware, or naive = UTC) as TikTok wants it: UTC+0 — `tz_name` is ignored (kept in
    the signature so every caller reads the same). Pure."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime(FMT)


def start_now(tz_name: str | None, lead_s: int = LEAD_S, now: datetime | None = None) -> str:
    """"Start now" for an ad group: UTC now + `lead_s` seconds (TikTok reads it in UTC+0)."""
    now = now or datetime.now(timezone.utc)
    return tiktok_time(now + timedelta(seconds=lead_s), tz_name)


START_BACK_MAX_MIN = 180
START_BACK_FLOOR_MIN = 60     # v155.34, the operator's standing rule: every ad group is scheduled at least ONE HOUR
                              # in the past, so no clock reading — TikTok's or ours — can ever hold a launch for later


def start_for(tz_name: str | None, back_min=START_BACK_FLOOR_MIN, now: datetime | None = None) -> str:
    """"Start now" moved `back_min` minutes EARLIER — never less than START_BACK_FLOOR_MIN (an hour),
    whatever the setting says. TikTok takes a start in the past as "start at once". Pure."""
    try:
        back = min(int(back_min or 0), START_BACK_MAX_MIN)
    except (TypeError, ValueError):
        back = 0
    back = max(back, START_BACK_FLOOR_MIN)
    return start_now(tz_name, lead_s=LEAD_S - back * 60, now=now)


def shown_in(tz_name: str | None, utc_str: str) -> str:
    """A TikTok UTC time ("YYYY-MM-DD HH:MM:SS") as Ads Manager will DISPLAY it for this account
    (its own timezone). Unknown zone → the UTC value. Pure."""
    try:
        when = datetime.strptime(utc_str[:19], FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return utc_str
    z = zone(tz_name)
    return when.astimezone(z).strftime(FMT) if z is not None else utc_str


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
