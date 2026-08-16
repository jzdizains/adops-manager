"""CAKE affiliate-network integration (optional — needs CAKE_API_URL + CAKE_API_KEY)."""
from __future__ import annotations

import httpx

from . import config

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def configured() -> bool:
    return bool(config.CAKE_API_URL and config.CAKE_API_KEY)


def conversions(start_date: str, end_date: str) -> dict:
    """CAKE export conversions endpoint. Dates MM/DD/YYYY per CAKE convention."""
    params = {
        "api_key": config.CAKE_API_KEY,
        "start_date": start_date,
        "end_date": end_date,
        "conversion_type": "all",
        "row_limit": 5000,
        "start_at_row": 1,
    }
    with httpx.Client(timeout=TIMEOUT) as c:
        resp = c.get(f"{config.CAKE_API_URL.rstrip('/')}/1/export.asmx/Conversions", params=params)
        resp.raise_for_status()
        return resp.json()


def pull_samples(start_date: str, end_date: str) -> list[dict]:
    data = conversions(start_date, end_date)
    out: dict[str, dict] = {}
    for conv in (data.get("d", {}) or {}).get("conversions", []) or data.get("conversions", []):
        sub = str(conv.get("sub_id_1") or conv.get("subid_1") or "")
        agg = out.setdefault(sub, {"sub_id": sub, "conversions": 0, "revenue": 0.0})
        agg["conversions"] += 1
        agg["revenue"] += float(conv.get("price", {}).get("amount", 0) if isinstance(conv.get("price"), dict)
                                else conv.get("price", 0) or 0)
    return list(out.values())
