"""Pixels — one inventory list with row actions.

Sync pulls every account's pixels via /pixel/list/. Each row then offers what
makes sense for it: an account-owned pixel can be MOVED into its BC (one-way,
per TikTok); a BC-owned pixel can be LINKED to one account or to every account
in the BC. Creation (with event) lives in a collapsed section.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, queries, tiktok_api
from ..database import get_db
from ..routes.launch import PIXEL_EVENTS
from ..templating import render

router = APIRouter()


def _link_pixel_to_bc_accounts(db: Session, token: str, bc_id: str,
                               pixel_id: str,
                               only: list[str] | None = None) -> tuple[int, list[str]]:
    """Link a BC-owned pixel to accounts under the BC (all enabled, or `only`).
    Batches of 20 with per-account fallback so one bad account is isolated."""
    targets = only if only is not None else [
        a.advertiser_id for a in queries.enabled_accounts(db)
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


def _upsert(db: Session, pixel_id: str, name: str = "", code: str = "",
            owner_adv: str = "", owner_bc: str = "") -> models.PixelRecord:
    row = db.query(models.PixelRecord).filter_by(pixel_id=pixel_id).first()
    if not row:
        row = models.PixelRecord(pixel_id=pixel_id)
        db.add(row)
    if name:
        row.pixel_name = name
    if code:
        row.pixel_code = code
    if owner_adv:
        row.owner_advertiser_id = owner_adv
    if owner_bc:
        row.owner_bc_id = owner_bc
    return row


@router.get("/pixels")
def pixels_page(request: Request, db: Session = Depends(get_db)):
    # one-time fold-in of legacy SharedPixel rows
    for sp in db.query(models.SharedPixel).all():
        _upsert(db, sp.pixel_id, name=sp.pixel_name, owner_bc=sp.bc_id)
    db.commit()

    pixels = (db.query(models.PixelRecord)
              .order_by(models.PixelRecord.pixel_name, models.PixelRecord.pixel_id).all())
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    accounts = queries.enabled_accounts(db)
    acct_names = {a.advertiser_id: (a.advertiser_name or a.advertiser_id) for a in accounts}
    bc_counts: dict[str, int] = {}
    for a in accounts:
        bc_counts[a.owner_bc_id] = bc_counts.get(a.owner_bc_id, 0) + 1

    rows = []
    for p in pixels:
        bc = bcs.get(p.owner_bc_id) if p.owner_bc_id else None
        # the BC a move would target (the owner account's BC)
        target_bc = None
        if not p.owner_bc_id and p.owner_advertiser_id:
            acct = next((a for a in accounts if a.advertiser_id == p.owner_advertiser_id), None)
            if acct and acct.owner_bc_id:
                target_bc = bcs.get(acct.owner_bc_id)
        rows.append({
            "p": p,
            "owner_label": (f"BC · {(bc.name or bc.bc_id)}" if bc
                            else acct_names.get(p.owner_advertiser_id,
                                                p.owner_advertiser_id or "—")),
            "is_bc": bool(p.owner_bc_id),
            "bc_account_count": bc_counts.get(p.owner_bc_id, 0),
            "target_bc": target_bc,
        })

    report = None
    raw = queries.get_setting(db, "pixel_provision_report", "")
    if raw:
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            pass
    return render(request, "pixels.html", {
        "title": "Pixels", "rows": rows, "accounts": accounts,
        "pixel_events": PIXEL_EVENTS, "provision_report": report,
        "synced_at": queries.get_setting(db, "pixels_synced_at", "")[:16].replace("T", " "),
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/pixels/sync")
def sync_pixels(db: Session = Depends(get_db)):
    """Pull every enabled account's pixels into the inventory."""
    import time as _time
    from datetime import datetime, timezone
    token = queries.any_access_token(db)
    if not token:
        return RedirectResponse("/pixels?err=Connect+TikTok+first", status_code=303)
    found, errors = 0, 0
    for acct in queries.enabled_accounts(db):
        try:
            for p in tiktok_api.list_pixels(acct.access_token, acct.advertiser_id):
                pid = str(p.get("pixel_id", ""))
                if not pid:
                    continue
                _upsert(db, pid, name=p.get("pixel_name", ""),
                        code=p.get("pixel_code", ""),
                        owner_adv=acct.advertiser_id)
                found += 1
        except tiktok_api.TikTokError:
            errors += 1
        _time.sleep(0.1)
    db.commit()
    queries.set_setting(db, "pixels_synced_at",
                        datetime.now(timezone.utc).isoformat())
    msg = f"Synced+{found}+pixel(s)"
    if errors:
        msg += f"&err={errors}+account(s)+failed"
    return RedirectResponse(f"/pixels?ok={msg}", status_code=303)


@router.post("/pixels/{record_id}/move-to-bc")
def move_to_bc(record_id: int, db: Session = Depends(get_db)):
    """Transfer an account-owned pixel into its account's Business Center."""
    p = db.get(models.PixelRecord, record_id)
    token = queries.any_access_token(db)
    if not p or not token or p.owner_bc_id:
        return RedirectResponse("/pixels?err=missing", status_code=303)
    acct = (db.query(models.AdAccount)
            .filter_by(advertiser_id=p.owner_advertiser_id).first())
    if not acct or not acct.owner_bc_id:
        return RedirectResponse("/pixels?err=Owner+account+has+no+BC+mapped+—+sync+first",
                                status_code=303)
    try:
        tiktok_api.bc_pixel_transfer(token, acct.owner_bc_id, acct.advertiser_id, p.pixel_id)
    except tiktok_api.TikTokError as e:
        return RedirectResponse(f"/pixels?err=Transfer+failed+(code+{e.code}:+"
                                f"{str(e.message)[:80].replace(' ', '+')})", status_code=303)
    p.owner_bc_id = acct.owner_bc_id
    db.commit()
    return RedirectResponse("/pixels?ok=Moved+into+the+BC+—+now+link+it+to+accounts",
                            status_code=303)


@router.post("/pixels/{record_id}/link-all")
def link_all(record_id: int, db: Session = Depends(get_db)):
    p = db.get(models.PixelRecord, record_id)
    token = queries.any_access_token(db)
    if not p or not token or not p.owner_bc_id:
        return RedirectResponse("/pixels?err=missing", status_code=303)
    ok_count, failed = _link_pixel_to_bc_accounts(db, token, p.owner_bc_id, p.pixel_id)
    msg = f"Linked+to+{ok_count}+account(s)"
    if failed:
        msg += f"&err=Failed:+{'+'.join(failed[:8])}"
    return RedirectResponse(f"/pixels?ok={msg}", status_code=303)


@router.post("/pixels/{record_id}/link-one")
def link_one(record_id: int, advertiser_id: str = Form(...), db: Session = Depends(get_db)):
    p = db.get(models.PixelRecord, record_id)
    token = queries.any_access_token(db)
    if not p or not token or not p.owner_bc_id:
        return RedirectResponse("/pixels?err=missing", status_code=303)
    ok_count, failed = _link_pixel_to_bc_accounts(db, token, p.owner_bc_id, p.pixel_id,
                                                  only=[advertiser_id])
    if failed:
        return RedirectResponse(f"/pixels?err=Link+failed:+{failed[0]}", status_code=303)
    return RedirectResponse("/pixels?ok=Linked", status_code=303)


@router.post("/pixels/{record_id}/rename")
def rename(record_id: int, pixel_name: str = Form(...), db: Session = Depends(get_db)):
    """Rename the pixel ON TIKTOK (and in our cache)."""
    p = db.get(models.PixelRecord, record_id)
    new_name = pixel_name.strip()[:128]
    if not p or not new_name:
        return RedirectResponse("/pixels?err=nothing+to+rename", status_code=303)
    acct = (db.query(models.AdAccount)
            .filter_by(advertiser_id=p.owner_advertiser_id).first())
    if not acct or not acct.access_token:
        return RedirectResponse(
            "/pixels?err=owner+account+not+connected+—+can't+rename+on+TikTok",
            status_code=303)
    try:
        tiktok_api.pixel_update(acct.access_token, acct.advertiser_id,
                                p.pixel_id, new_name)
    except tiktok_api.TikTokError as e:
        return RedirectResponse(
            f"/pixels?err=TikTok+refused+rename+(code+{e.code})", status_code=303)
    p.pixel_name = new_name
    db.commit()
    return RedirectResponse("/pixels?ok=Renamed+on+TikTok", status_code=303)


@router.post("/pixels/{record_id}/delete")
def remove(record_id: int, db: Session = Depends(get_db)):
    p = db.get(models.PixelRecord, record_id)
    if p:
        db.delete(p)
        db.commit()
    return RedirectResponse("/pixels?ok=Removed+from+the+list+(TikTok+unchanged)", status_code=303)


# ---------------------------------------------------------------------------
# Create (collapsed section) — pixel + chosen event, optional BC share
# ---------------------------------------------------------------------------

@router.post("/pixels/provision")
def provision(pixel_name: str = Form(...), advertiser_id: str = Form(...),
              event_type: str = Form(...), event_name: str = Form(""),
              do_share: str = Form(""), db: Session = Depends(get_db)):
    token = queries.any_access_token(db)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not token or not acct:
        return RedirectResponse("/pixels?err=Connect+TikTok+first", status_code=303)
    share = do_share == "on"
    steps: list[dict] = []
    pixel_id = ""

    try:
        data = tiktok_api.pixel_create(token, advertiser_id, pixel_name.strip())
        pixel_id = str(data.get("pixel_id", "") or (data.get("pixel", {}) or {}).get("pixel_id", ""))
        pixel_code = str(data.get("pixel_code", "") or (data.get("pixel", {}) or {}).get("pixel_code", ""))
        steps.append({"step": "Create pixel", "ok": True,
                      "detail": f"pixel_id {pixel_id}" + (f" · code {pixel_code}" if pixel_code else "")})
        _upsert(db, pixel_id, name=pixel_name.strip(), code=pixel_code,
                owner_adv=advertiser_id)
        db.commit()
    except tiktok_api.TikTokError as e:
        steps.append({"step": "Create pixel", "ok": False,
                      "detail": f"code {e.code}: {str(e.message)[:160]}"})
        queries.set_setting(db, "pixel_provision_report",
                            json.dumps({"name": pixel_name, "steps": steps}))
        return RedirectResponse("/pixels?err=Pixel+creation+failed+—+see+the+report", status_code=303)

    labels = dict(PIXEL_EVENTS)
    if event_type in labels:
        try:
            tiktok_api.pixel_event_create(token, advertiser_id, pixel_id, [{
                "event_type": event_type,
                "event_name": event_name.strip() or labels[event_type]}])
            steps.append({"step": f"Create event ({labels[event_type]})", "ok": True,
                          "detail": f"event_type {event_type}"})
        except tiktok_api.TikTokError as e:
            steps.append({"step": f"Create event ({labels.get(event_type, event_type)})", "ok": False,
                          "detail": f"code {e.code}: {str(e.message)[:160]} — you can add the event "
                                    "in TikTok Events Manager instead"})

    if share and acct.owner_bc_id:
        try:
            tiktok_api.bc_pixel_transfer(token, acct.owner_bc_id, advertiser_id, pixel_id)
            steps.append({"step": "Transfer to Business Center", "ok": True,
                          "detail": f"BC {acct.owner_bc_id}"})
            rec = db.query(models.PixelRecord).filter_by(pixel_id=pixel_id).first()
            if rec:
                rec.owner_bc_id = acct.owner_bc_id
                db.commit()
            ok_count, failed = _link_pixel_to_bc_accounts(db, token, acct.owner_bc_id, pixel_id)
            steps.append({"step": "Link to all BC accounts", "ok": not failed,
                          "detail": f"linked {ok_count}"
                          + (f" · failed: {', '.join(failed[:6])}" if failed else "")})
        except tiktok_api.TikTokError as e:
            steps.append({"step": "Transfer to Business Center", "ok": False,
                          "detail": f"code {e.code}: {str(e.message)[:160]}"})
    elif share:
        steps.append({"step": "Transfer to Business Center", "ok": False,
                      "detail": "the account has no Business Center mapped — sync first"})

    queries.set_setting(db, "pixel_provision_report",
                        json.dumps({"name": pixel_name, "pixel_id": pixel_id, "steps": steps}))
    return RedirectResponse("/pixels?ok=Pixel+created+—+see+the+step+report", status_code=303)
