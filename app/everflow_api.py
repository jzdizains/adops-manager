"""Everflow affiliate-network integration (optional — needs EVERFLOW_API_KEY)."""
from __future__ import annotations

import httpx

from . import config

BASE = "https://api.eflow.team/v1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def configured() -> bool:
    return bool(config.EVERFLOW_API_KEY)


def _headers() -> dict:
    return {"X-Eflow-API-Key": config.EVERFLOW_API_KEY, "Content-Type": "application/json"}


def reporting_entity(start_date: str, end_date: str, columns: list[str] | None = None) -> dict:
    """Aggregate conversions/revenue between dates (dates are YYYY-MM-DD)."""
    payload = {
        "from": start_date, "to": end_date,
        "timezone_id": 67,  # America/New_York; adjust to the business TZ id
        "currency_id": "USD",
        "columns": [{"column": c} for c in (columns or ["sub1"])],
        "query": {"filters": []},
    }
    with httpx.Client(timeout=TIMEOUT) as c:
        resp = c.post(f"{BASE}/affiliates/reporting/entity", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def pull_samples(start_date: str, end_date: str) -> list[dict]:
    """Normalize to [{sub_id, conversions, revenue}] for ConversionSample rows."""
    data = reporting_entity(start_date, end_date)
    out = []
    for row in data.get("table", []):
        cols = {c.get("column_type"): c.get("label") for c in row.get("columns", [])}
        rep = row.get("reporting", {})
        out.append({
            "sub_id": cols.get("sub1", "") or "",
            "conversions": int(rep.get("cv", 0) or 0),
            "revenue": float(rep.get("revenue", 0) or 0),
        })
    return out
