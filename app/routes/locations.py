"""Locations reference — pull the targetable location list (numeric IDs) straight
from TikTok for any connected account, so presets never need guessed IDs.

`/tool/region/` returns the EXACT ids TikTok accepts for that account,
objective and placement — the authoritative source (country ids follow the
GeoNames scheme, e.g. 6252001 = United States)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import models, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()

OBJECTIVES = ["TRAFFIC", "WEB_CONVERSIONS", "REACH", "VIDEO_VIEWS",
              "LEAD_GENERATION", "APP_PROMOTION", "PRODUCT_SALES", "ENGAGEMENT"]


def _norm(item: dict) -> dict:
    """TikTok has shipped several field spellings — normalize defensively."""
    return {
        "id": str(item.get("location_id") or item.get("region_id") or item.get("id") or ""),
        "name": item.get("name") or item.get("region_name") or "",
        "level": item.get("level") or item.get("area_type") or "",
        "region_code": item.get("region_code") or "",
        "parent_id": str(item.get("parent_id") or ""),
    }


@router.get("/locations")
def locations_page(request: Request, db: Session = Depends(get_db)):
    accounts = (db.query(models.AdAccount)
                .filter(models.AdAccount.enabled == True)  # noqa: E712
                .filter(models.AdAccount.access_token != "")
                .order_by(models.AdAccount.advertiser_name).all())
    advertiser_id = request.query_params.get("advertiser_id", "")
    objective = request.query_params.get("objective", "TRAFFIC")
    if objective not in OBJECTIVES:
        objective = "TRAFFIC"
    rows: list[dict] = []
    err = ""
    if advertiser_id:
        acct = next((a for a in accounts if a.advertiser_id == advertiser_id), None)
        if not acct:
            err = "Pick one of the connected accounts."
        else:
            try:
                raw = tiktok_api.list_regions(
                    acct.access_token, acct.advertiser_id,
                    placements=["PLACEMENT_TIKTOK"], objective_type=objective)
                rows = [r for r in (_norm(i) for i in raw) if r["id"]]
                # countries first, then alphabetical — the list can be thousands long
                level_rank = {"COUNTRY": 0, "PROVINCE": 1, "STATE": 1,
                              "DMA": 2, "CITY": 3}
                rows.sort(key=lambda r: (level_rank.get(str(r["level"]).upper(), 9),
                                         r["name"]))
            except tiktok_api.TikTokError as e:
                err = f"TikTok couldn't return locations (code {e.code}): {e.message}"
    return render(request, "locations.html", {
        "accounts": accounts, "advertiser_id": advertiser_id,
        "objective": objective, "objectives": OBJECTIVES,
        "rows": rows, "err": err, "title": "Locations",
    })
