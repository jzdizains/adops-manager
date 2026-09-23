"""/warmup — warm up ad accounts: a small Reach campaign per account that pauses itself
the moment TikTok approves the ad (see app/warmup.py).

The launch goes through the ONE launch engine (queue_launch → launch_to_account), so a
warm-up gets the same identity resolution, result page, error explanations and retry
rules as any launch. This page adds the recipe (Reach, budget, country) and the watch list
of warm-ups with their live state — it updates itself while any is waiting.
"""
from __future__ import annotations

import json
import math
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models, warmup
from .. import scope as scope_mod
from ..database import get_db
from ..templating import render

router = APIRouter()


def _back(err: str) -> RedirectResponse:
    return RedirectResponse("/warmup?err=" + quote(err), status_code=303)


def _rows(db: Session, sc, limit: int = 100) -> list[models.LaunchLog]:
    q = (db.query(models.LaunchLog)
         .filter(models.LaunchLog.warmup == True)                      # noqa: E712
         .order_by(models.LaunchLog.id.desc()).limit(400))
    return [r for r in q if sc.allows(r.advertiser_id)][:limit]


def _row_json(r: models.LaunchLog) -> dict:
    state = r.warmup_state if r.ok else "failed"
    return {"id": r.id, "state": state,
            "label": warmup.STATE_LABELS.get(state, "Launch failed" if state == "failed" else state),
            "spend": round(r.warmup_spend or 0.0, 2),
            "done_at": r.warmup_done_at.isoformat() + "Z" if r.warmup_done_at else ""}


@router.get("/warmup")
def warmup_page(request: Request, db: Session = Depends(get_db)):
    from .super_launcher import picker_prefs, profile_bcs
    sc = scope_mod.for_request(request, db)
    accounts = [a for a in (db.query(models.AdAccount).filter(models.AdAccount.enabled == True)   # noqa: E712
                            .order_by(models.AdAccount.advertiser_name).all()) if sc.allows(a.advertiser_id)]
    sparks = (sc.owned(db.query(models.SparkCode), models.SparkCode).filter_by(status="active")
              .order_by(models.SparkCode.name).limit(500).all())
    countries = (db.query(models.RegionName).filter(models.RegionName.region_code != "")
                 .order_by(models.RegionName.name).limit(400).all())
    countries = [c for c in countries if str(c.level or "").upper() == "COUNTRY"]
    rows = _rows(db, sc)
    counts = {k: sum(1 for r in rows if r.ok and r.warmup_state == k) for k in ("waiting", "paused", "rejected")}
    return render(request, "warmup.html", {
        "title": "Warm up", "active": "launch",
        "accounts_n": len(accounts), "sparks": sparks, "countries": countries,
        "rows": rows, "counts": counts, "labels": warmup.STATE_LABELS,
        "default_budget": warmup.DEFAULT_BUDGET, "min_budget": warmup.MIN_BUDGET,
        "bcs_json": json.dumps(profile_bcs(db, accounts)),
        **picker_prefs(db, sc),
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.get("/warmup/state.json")
def warmup_state(request: Request, db: Session = Depends(get_db)):
    """Tiny poll for the watch list: state of each warm-up in view. No TikTok calls."""
    sc = scope_mod.for_request(request, db)
    rows = _rows(db, sc, limit=60)
    return JSONResponse({"ok": True, "rows": [_row_json(r) for r in rows],
                         "waiting": sum(1 for r in rows if r.ok and r.warmup_state == "waiting")})


@router.post("/warmup/launch")
async def warmup_launch(request: Request, db: Session = Depends(get_db)):
    from .. import profile_videos
    from . import campaigns as engine
    sc = scope_mod.for_request(request, db)
    form = await request.form()

    seen: set = set()
    ids = [a for a in form.getlist("advertiser_ids")
           if a and sc.allows(a) and not (a in seen or seen.add(a))]          # only the view's accounts
    by_id = {a.advertiser_id: a for a in db.query(models.AdAccount)
             .filter(models.AdAccount.advertiser_id.in_(ids)).all()} if ids else {}
    accounts = [by_id[i] for i in ids if i in by_id]
    if not accounts:
        return _back("Pick at least one ad account to warm up.")

    geo = form.get("geo") or "account"
    locations = [x for x in form.getlist("location_ids") if str(x).strip().isdigit()]
    if geo == "list" and not locations:
        return _back("Pick at least one country, or target each account's own country.")

    # the post(s): profile posts picked in the picker, or one spark code
    sparks: list = []
    try:
        items = json.loads(form.get("profile_items") or "[]")
    except ValueError:
        items = []
    items = [it for it in items if isinstance(it, dict) and str(it.get("item_id") or "").isdigit()][:50]
    if items:
        sparks = profile_videos.ensure_spark_rows(db, sc, items)
    elif str(form.get("spark_code_id") or "").isdigit():
        row = db.get(models.SparkCode, int(form.get("spark_code_id")))
        if row is None or not sc.owns(row):
            return _back("That spark code isn't in your workspace.")
        sparks = [row]
    if not sparks:
        return _back("Pick the post the warm-up runs.")

    fields = warmup.warmup_fields(form.get("budget"), own_country=(geo != "list"), location_ids=locations)
    fields["_launched_by"] = sc.owner_for_new
    per = max(1, math.ceil(len(accounts) / len(sparks)))           # one post covers every account; several are spread evenly
    pairs = profile_videos.assign(accounts, sparks, per)
    ref = engine.queue_launch(db, f"Warm-up → {len(pairs)} account(s)",
                              [a.advertiser_id for a, _ in pairs], fields,
                              spark_pairs=[[a.advertiser_id, sid] for a, sid in pairs])
    return RedirectResponse(f"/campaigns/result/{ref}", status_code=303)
