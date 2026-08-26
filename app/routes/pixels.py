"""Pixel sharing automation.

Flow per §API (verified endpoints):
  1. A pixel lives on one ad account. `bc/pixel/transfer/` moves it into
     Business Center ownership.
  2. `bc/pixel/link/update/` links the BC-owned pixel to any of the BC's
     ad accounts — the tool links it to ALL of them in one click.
  3. `bc/pixel/link/get/` shows current links (health view).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

import json

from .. import error_messages, models, queries, tiktok_api
from ..database import get_db
from ..routes.launch import PIXEL_EVENTS
from ..templating import render

router = APIRouter()


def _link_pixel_to_bc_accounts(db: Session, token: str, bc_id: str,
                               pixel_id: str) -> tuple[int, list[str]]:
    """Link a BC-owned pixel to every enabled account under the BC.
    Batches of 20 with per-account fallback so one bad account is isolated."""
    targets = [a.advertiser_id for a in queries.enabled_accounts(db)
               if a.owner_bc_id == bc_id]
    ok_count, failed = 0, []
    for i in range(0, len(targets), 20):
        batch = targets[i:i + 20]
        try:
            tiktok_api.bc_pixel_link_update(token, bc_id, pixel_id, batch, "LINK")
            ok_count += len(batch)
        except tiktok_api.TikTokError:
            for adv in batch:
                try:
                    tiktok_api.bc_pixel_link_update(token, bc_id, pixel_id, [adv], "LINK")
                    ok_count += 1
                except tiktok_api.TikTokError as e:
                    failed.append(f"{adv}({e.code})")
    return ok_count, failed


@router.get("/pixels")
def pixels_page(request: Request, db: Session = Depends(get_db)):
    bcs = db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()
    shared = db.query(models.SharedPixel).order_by(models.SharedPixel.created_at.desc()).all()
    accounts = queries.enabled_accounts(db)
    counts = {}
    for a in accounts:
        counts[a.owner_bc_id] = counts.get(a.owner_bc_id, 0) + 1
    provision_report = None
    raw = queries.get_setting(db, "pixel_provision_report", "")
    if raw:
        try:
            provision_report = json.loads(raw)
        except json.JSONDecodeError:
            pass
    return render(request, "pixels.html", {
        "title": "Pixels", "bcs": bcs, "shared": shared, "counts": counts,
        "accounts": accounts, "pixel_events": PIXEL_EVENTS,
        "provision_report": provision_report,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/pixels/provision")
def provision(pixel_name: str = Form(...), advertiser_id: str = Form(...),
              event_type: str = Form(...), event_name: str = Form(""),
              do_share: str = Form(""), db: Session = Depends(get_db)):
    """The full pipeline: create pixel → create event → transfer to BC →
    link to every account in the BC. Each step's outcome is recorded and
    shown — a TikTok complaint at any step is visible, never silent."""
    token = queries.any_access_token(db)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not token or not acct:
        return RedirectResponse("/pixels?err=Connect+TikTok+first", status_code=303)
    share = do_share == "on"
    steps: list[dict] = []
    pixel_id = ""

    # 1 · create the pixel
    try:
        data = tiktok_api.pixel_create(token, advertiser_id, pixel_name.strip())
        pixel_id = str(data.get("pixel_id", "") or (data.get("pixel", {}) or {}).get("pixel_id", ""))
        pixel_code = str(data.get("pixel_code", "") or (data.get("pixel", {}) or {}).get("pixel_code", ""))
        steps.append({"step": "Create pixel", "ok": True,
                      "detail": f"pixel_id {pixel_id}" + (f" · code {pixel_code}" if pixel_code else "")})
    except tiktok_api.TikTokError as e:
        steps.append({"step": "Create pixel", "ok": False,
                      "detail": f"code {e.code}: {str(e.message)[:160]}"})
        queries.set_setting(db, "pixel_provision_report",
                            json.dumps({"name": pixel_name, "steps": steps}))
        return RedirectResponse("/pixels?err=Pixel+creation+failed+—+see+the+report+below", status_code=303)

    # 2 · create the selected event on it
    labels = dict(PIXEL_EVENTS)
    if event_type in labels:
        event_payload = {"event_type": event_type,
                         "event_name": event_name.strip() or labels[event_type]}
        try:
            tiktok_api.pixel_event_create(token, advertiser_id, pixel_id, [event_payload])
            steps.append({"step": f"Create event ({labels[event_type]})", "ok": True,
                          "detail": f"event_type {event_type}"})
        except tiktok_api.TikTokError as e:
            steps.append({"step": f"Create event ({labels.get(event_type, event_type)})", "ok": False,
                          "detail": f"code {e.code}: {str(e.message)[:160]} — you can add the event "
                                    "in TikTok Events Manager instead"})

    # 3+4 · share across the BC
    bc_id = acct.owner_bc_id
    if share and bc_id:
        try:
            tiktok_api.bc_pixel_transfer(token, bc_id, advertiser_id, pixel_id)
            steps.append({"step": "Transfer to Business Center", "ok": True, "detail": f"BC {bc_id}"})
            transferred = True
        except tiktok_api.TikTokError as e:
            steps.append({"step": "Transfer to Business Center", "ok": False,
                          "detail": f"code {e.code}: {str(e.message)[:160]}"})
            transferred = False
        if transferred:
            if not db.query(models.SharedPixel).filter_by(bc_id=bc_id, pixel_id=pixel_id).first():
                db.add(models.SharedPixel(bc_id=bc_id, pixel_id=pixel_id,
                                          pixel_name=pixel_name.strip()))
                db.commit()
            ok_count, failed = _link_pixel_to_bc_accounts(db, token, bc_id, pixel_id)
            steps.append({"step": "Link to all BC accounts",
                          "ok": not failed, "detail": f"linked {ok_count}"
                          + (f" · failed: {', '.join(failed[:6])}" if failed else "")})
    elif share and not bc_id:
        steps.append({"step": "Transfer to Business Center", "ok": False,
                      "detail": "the seed account has no Business Center mapped — sync first"})

    queries.set_setting(db, "pixel_provision_report",
                        json.dumps({"name": pixel_name, "pixel_id": pixel_id, "steps": steps}))
    all_ok = all(s["ok"] for s in steps)
    return RedirectResponse(
        "/pixels?ok=Pixel+created" + ("+and+shared" if share and all_ok else "+—+see+step+report"),
        status_code=303)


@router.post("/pixels/transfer")
def transfer(pixel_id: str = Form(...), owner_advertiser_id: str = Form(...),
             bc_id: str = Form(...), pixel_name: str = Form(""),
             db: Session = Depends(get_db)):
    """Move a pixel from its owner ad account into BC ownership."""
    token = queries.any_access_token(db)
    if not token:
        return RedirectResponse("/pixels?err=Connect+TikTok+first", status_code=303)
    try:
        tiktok_api.bc_pixel_transfer(token, bc_id, owner_advertiser_id.strip(), pixel_id.strip())
    except tiktok_api.TikTokError as e:
        info = error_messages.explain(e.code, e.message)
        return RedirectResponse(f"/pixels?err={info['friendly'][:120]}+({e.code})", status_code=303)
    if not db.query(models.SharedPixel).filter_by(bc_id=bc_id, pixel_id=pixel_id.strip()).first():
        db.add(models.SharedPixel(bc_id=bc_id, pixel_id=pixel_id.strip(),
                                  pixel_name=pixel_name.strip()))
        db.commit()
    return RedirectResponse("/pixels?ok=Pixel+transferred+to+BC+ownership", status_code=303)


@router.post("/pixels/register")
def register(pixel_id: str = Form(...), bc_id: str = Form(...), pixel_name: str = Form(""),
             db: Session = Depends(get_db)):
    """Register a pixel that is ALREADY BC-owned (skip the transfer step)."""
    if not db.query(models.SharedPixel).filter_by(bc_id=bc_id, pixel_id=pixel_id.strip()).first():
        db.add(models.SharedPixel(bc_id=bc_id, pixel_id=pixel_id.strip(),
                                  pixel_name=pixel_name.strip()))
        db.commit()
    return RedirectResponse("/pixels?ok=Pixel+registered", status_code=303)


@router.post("/pixels/{shared_id}/link-all")
def link_all(shared_id: int, db: Session = Depends(get_db)):
    """Link a BC-owned pixel to EVERY enabled ad account under that BC."""
    sp = db.get(models.SharedPixel, shared_id)
    token = queries.any_access_token(db)
    if not sp or not token:
        return RedirectResponse("/pixels?err=missing", status_code=303)
    ok_count, failed = _link_pixel_to_bc_accounts(db, token, sp.bc_id, sp.pixel_id)
    if ok_count == 0 and not failed:
        return RedirectResponse("/pixels?err=No+enabled+accounts+under+that+BC", status_code=303)
    msg = f"Linked+to+{ok_count}+account(s)"
    if failed:
        msg += f"&err=Failed:+{'+'.join(failed[:8])}"
    return RedirectResponse(f"/pixels?ok={msg}", status_code=303)


@router.post("/pixels/{shared_id}/delete")
def remove(shared_id: int, db: Session = Depends(get_db)):
    sp = db.get(models.SharedPixel, shared_id)
    if sp:
        db.delete(sp)
        db.commit()
    return RedirectResponse("/pixels?ok=Removed+from+the+list+(links+on+TikTok+unchanged)", status_code=303)
