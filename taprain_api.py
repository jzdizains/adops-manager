"""Taprain affiliate-network integration (optional — needs TAPRAIN_API_KEY)."""
from __future__ import annotations

import httpx

from . import config

BASE = "https://api.taprain.com/v1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def configured() -> bool:
    return bool(config.TAPRAIN_API_KEY)


def stats(start_date: str, end_date: str) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        resp = c.get(f"{BASE}/reports/conversions", params={
            "start_date": start_date, "end_date": end_date,
        }, headers={"Authorization": f"Bearer {config.TAPRAIN_API_KEY}"})
        resp.raise_for_status()
        return resp.json()


def pull_samples(start_date: str, end_date: str) -> list[dict]:
    data = stats(start_date, end_date)
    out = []
    for row in data.get("data", []):
        out.append({
            "sub_id": str(row.get("sub_id", "")),
            "conversions": int(row.get("conversions", 0) or 0),
            "revenue": float(row.get("revenue", 0) or 0),
        })
    return out
