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

from .. import error_messages, models, queries, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/pixels")
def pixels_page(request: Request, db: Session = Depends(get_db)):
    bcs = db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()
    shared = db.query(models.SharedPixel).order_by(models.SharedPixel.created_at.desc()).all()
    accounts = queries.enabled_accounts(db)
    counts = {}
    for a in accounts:
        counts[a.owner_bc_id] = counts.get(a.owner_bc_id, 0) + 1
    return render(request, "pixels.html", {
        "title": "Pixels", "bcs": bcs, "shared": shared, "counts": counts,
        "accounts": accounts,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


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
    targets = [a.advertiser_id for a in queries.enabled_accounts(db)
               if a.owner_bc_id == sp.bc_id]
    if not targets:
        return RedirectResponse("/pixels?err=No+enabled+accounts+under+that+BC", status_code=303)
    ok_count, failed = 0, []
    # link in batches of 20 — one bad account can't sink the rest
    for i in range(0, len(targets), 20):
        batch = targets[i:i + 20]
        try:
            tiktok_api.bc_pixel_link_update(token, sp.bc_id, sp.pixel_id, batch, "LINK")
            ok_count += len(batch)
        except tiktok_api.TikTokError:
            for adv in batch:   # retry singly to find the culprits
                try:
                    tiktok_api.bc_pixel_link_update(token, sp.bc_id, sp.pixel_id, [adv], "LINK")
                    ok_count += 1
                except tiktok_api.TikTokError as e:
                    failed.append(f"{adv}({e.code})")
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
