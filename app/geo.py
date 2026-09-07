"""IP → location for the access log (ipinfo.io, cached 30 days per IP).
Private / local addresses are labelled without a lookup. Any failure is
cached briefly as an error so a dead service can't slow the page down."""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from . import config, models

TTL = timedelta(days=30)
ERR_TTL = timedelta(hours=1)
MAX_LOOKUPS_PER_RENDER = 8


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def is_private(ip: str) -> bool:
    """RFC 1918 / loopback / link-local — never worth a lookup. (Python also
    flags the documentation ranges 192.0.2/198.51.100/203.0.113 as private.)"""
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
    except ValueError:
        return False


def fetch(ip: str) -> dict:
    """One ipinfo.io call. Returns {city, region, country, org, timezone} or {'error': …}."""
    params = {"token": config.IPINFO_TOKEN} if config.IPINFO_TOKEN else {}
    try:
        r = httpx.get(f"https://ipinfo.io/{ip}/json", params=params, timeout=httpx.Timeout(4.0, connect=2.0),
                      headers={"Accept": "application/json"})
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        j = r.json()
        return {"city": j.get("city") or "", "region": j.get("region") or "", "country": j.get("country") or "",
                "org": j.get("org") or "", "timezone": j.get("timezone") or ""}
    except (httpx.HTTPError, ValueError) as e:
        return {"error": str(e)[:100]}


def lookup(db: Session, ip: str, budget: list[int] | None = None) -> models.IpInfo | None:
    """Cached row for the ip; looks it up when missing/stale while the
    per-render budget allows. None for private IPs."""
    if not ip or not is_ip(ip) or is_private(ip):
        return None
    row = db.query(models.IpInfo).filter_by(ip=ip).first()
    fresh = row is not None and row.looked_up_at and (_now() - row.looked_up_at) < (ERR_TTL if row.error else TTL)
    if fresh:
        return row
    if budget is not None:
        if budget[0] <= 0:
            return row
        budget[0] -= 1
    data = fetch(ip)
    if not row:
        row = models.IpInfo(ip=ip)
        db.add(row)
    row.city, row.region, row.country = data.get("city", ""), data.get("region", ""), data.get("country", "")
    row.org, row.timezone, row.error = data.get("org", ""), data.get("timezone", ""), data.get("error", "")
    row.looked_up_at = _now()
    db.commit()
    return row


def label(row: models.IpInfo | None, ip: str = "") -> str:
    if row is None:
        return "local / private network" if (ip and is_private(ip)) else ""
    if row.error and not row.country:
        return ""
    parts = [p for p in (row.city, row.region, row.country) if p]
    return ", ".join(parts)
