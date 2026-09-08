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

from .. import jobs, models, queries, tiktok_api
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


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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


REPORT_TTL_H = 24


def _fresh_report(raw: str) -> dict | None:
    """A stored run report, if it is less than a day old (older ones — and the legacy
    ones without a timestamp — are not shown; the row list is the truth anyway)."""
    from datetime import datetime, timedelta, timezone
    if not raw:
        return None
    try:
        rep = json.loads(raw)
        at = datetime.fromisoformat(rep.get("at", ""))
    except (ValueError, TypeError, AttributeError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return rep if datetime.now(timezone.utc) - at < timedelta(hours=REPORT_TTL_H) else None


@router.post("/pixels/report/dismiss")
def dismiss_report(which: str = Form("provision"), db: Session = Depends(get_db)):
    key = "pixel_sync_report" if which == "sync" else "pixel_provision_report"
    queries.set_setting(db, key, "")
    return RedirectResponse("/pixels", status_code=303)


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

    # run reports are shown for a day (or until dismissed) — not forever
    report = _fresh_report(queries.get_setting(db, "pixel_provision_report", ""))
    sync_report = _fresh_report(queries.get_setting(db, "pixel_sync_report", ""))
    return render(request, "pixels.html", {
        "title": "Pixels", "rows": rows, "accounts": accounts, "sync_report": sync_report,
        "bc_list": sorted(bcs.values(), key=lambda b: (b.name or b.bc_id).lower()),
        "sync_pending": jobs.pending(db, "pixels_sync") is not None,
        "pixel_events": PIXEL_EVENTS, "provision_report": report,
        "synced_at": queries.get_setting(db, "pixels_synced_at", ""),
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


def _scope(db: Session, scope: str) -> tuple[list[models.AdAccount], str]:
    """`all` | `bc:<bc id>` | `acct:<advertiser id>` → the enabled accounts to pull from + a label."""
    accounts = queries.enabled_accounts(db)
    if scope.startswith("bc:"):
        bc_id = scope[3:]
        bc = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
        return [a for a in accounts if a.owner_bc_id == bc_id], f"BC {(bc.name if bc else '') or bc_id}"
    if scope.startswith("acct:"):
        adv = scope[5:]
        acct = next((a for a in accounts if a.advertiser_id == adv), None)
        return ([acct] if acct else []), (acct.advertiser_name or adv) if acct else adv
    return accounts, "every account"


@router.post("/pixels/sync")
def sync_pixels(scope: str = Form("all"), db: Session = Depends(get_db)):
    """Queue: pull pixels from every enabled account, one Business Center's accounts, or one account."""
    from urllib.parse import quote
    if not queries.any_access_token(db):
        return RedirectResponse("/pixels?err=Connect+TikTok+first", status_code=303)
    accounts, label = _scope(db, scope)
    if not accounts:
        return RedirectResponse("/pixels?err=" + quote(f"No enabled ad account under {label} to sync from."), status_code=303)
    job, created = jobs.enqueue_once(db, "pixels_sync", f"Sync pixels — {label}", {"scope": scope}, href="/pixels")
    if not created:
        return RedirectResponse(f"/pixels?ok=A+sync+is+already+{job.status}+—+hold+on.", status_code=303)
    return RedirectResponse("/pixels?ok=" + quote(f"Syncing pixels from {label} ({len(accounts)} account(s)) — the list refreshes when it's done."), status_code=303)


def sync_pixels_inventory(db: Session, scope: str = "all") -> dict:
    """The pixel sync itself (runs in a job). Every enabled account's /pixel/list/ is
    pulled; the result — distinct pixels, accounts that answered, and WHY any account
    failed (TikTok's code + message, explained) — is saved as `pixel_sync_report` so
    the Pixels page can show it instead of a bare "N failed"."""
    import time as _time
    from datetime import datetime, timezone
    from .. import error_messages
    seen: set[str] = set()
    ok_accounts, failures = 0, []
    accounts, label = _scope(db, scope)
    for acct in accounts:
        if not acct.access_token:
            failures.append({"account": acct.advertiser_name or acct.advertiser_id, "advertiser_id": acct.advertiser_id,
                             "code": "no-token", "message": "no access token on this account — reconnect TikTok",
                             "friendly": "Not connected.", "action": "Reconnect TikTok (Ad accounts → Connect)."})
            continue
        try:
            for p in tiktok_api.list_pixels(acct.access_token, acct.advertiser_id):
                pid = str(p.get("pixel_id", ""))
                if not pid:
                    continue
                _upsert(db, pid, name=p.get("pixel_name", ""),
                        code=p.get("pixel_code", ""),
                        owner_adv=acct.advertiser_id)
                seen.add(pid)
            ok_accounts += 1
        except tiktok_api.TikTokError as e:
            ex = error_messages.explain(e.code, e.message)
            failures.append({"account": acct.advertiser_name or acct.advertiser_id, "advertiser_id": acct.advertiser_id,
                             "code": str(e.code), "message": (e.message or "")[:300],
                             "friendly": ex["friendly"], "action": ex["action"]})
        _time.sleep(0.1)
    db.commit()
    report = {"at": datetime.now(timezone.utc).isoformat(), "pixels": len(seen), "accounts": len(accounts),
              "ok_accounts": ok_accounts, "failures": failures[:50], "scope": scope, "label": label}
    queries.set_setting(db, "pixel_sync_report", json.dumps(report))
    queries.set_setting(db, "pixels_synced_at", report["at"])
    return report


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
    from .. import jobs
    jobs.enqueue(db, "pixel_link_all", f"Link pixel {p.pixel_name or p.pixel_id} to every account of its BC",
                 {"record_id": record_id}, href="/pixels")
    return RedirectResponse("/pixels?ok=Linking+in+the+background+—+you%27ll+get+a+notification.", status_code=303)


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
# Add a pixel that already exists in Ads Manager — by Pixel ID or pixel code.
# Checked against TikTok (/pixel/list/ with the id/code filter) on the chosen
# account, or on every connected account, then stored like a synced one.
# ---------------------------------------------------------------------------

@router.post("/pixels/add-existing")
def add_existing(pixel_ref: str = Form(""), advertiser_id: str = Form(""), db: Session = Depends(get_db)):
    from urllib.parse import quote
    from .. import error_messages
    ref = (pixel_ref or "").strip()
    if not ref or not ref.replace("_", "").replace("-", "").isalnum() or len(ref) > 64:
        return RedirectResponse("/pixels?err=" + quote("Paste the Pixel ID (digits) or the pixel code from Ads Manager → Events."), status_code=303)
    if db.query(models.PixelRecord).filter(
            (models.PixelRecord.pixel_id == ref) | (models.PixelRecord.pixel_code == ref)).first():
        return RedirectResponse("/pixels?ok=" + quote(f"{ref} is already in the list."), status_code=303)
    accounts = queries.enabled_accounts(db)
    if advertiser_id:
        accounts = [a for a in accounts if a.advertiser_id == advertiser_id]
    if not accounts:
        return RedirectResponse("/pixels?err=" + quote("No connected ad account to check the pixel on — connect TikTok first."), status_code=303)
    by_id = ref.isdigit()
    errors: list[str] = []
    for acct in accounts:
        if not acct.access_token:
            continue
        try:
            found = tiktok_api.list_pixels(acct.access_token, acct.advertiser_id, **({"pixel_id": ref} if by_id else {"code": ref}))
        except tiktok_api.TikTokError as e:
            ex = error_messages.explain(e.code, e.message)
            errors.append(f"{acct.advertiser_name or acct.advertiser_id}: {ex['friendly']} (code {e.code})")
            continue
        for p in found:
            pid, pcode = str(p.get("pixel_id", "")), str(p.get("pixel_code", ""))
            if (by_id and pid == ref) or (not by_id and pcode.lower() == ref.lower()):
                row = _upsert(db, pid, name=p.get("pixel_name", ""), code=pcode, owner_adv=acct.advertiser_id)
                db.commit()
                return RedirectResponse("/pixels?ok=" + quote(f"Added “{row.pixel_name or pid}” (pixel {pid}) from {acct.advertiser_name or acct.advertiser_id}."), status_code=303)
    where = "that account" if advertiser_id else f"any of your {len(accounts)} connected account(s)"
    msg = (f"TikTok doesn't list pixel {ref} on {where}. It lives on an ad account that isn't connected here, "
           "or it's a Business Center pixel that isn't linked to these accounts yet (Business Center → Assets → Pixels → link).")
    if errors:
        msg += " Also: " + "; ".join(errors[:3])
    return RedirectResponse("/pixels?err=" + quote(msg), status_code=303)


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
                            json.dumps({"name": pixel_name, "steps": steps, "at": _now_iso()}))
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
                        json.dumps({"name": pixel_name, "pixel_id": pixel_id, "steps": steps, "at": _now_iso()}))
    return RedirectResponse("/pixels?ok=Pixel+created+—+see+the+step+report", status_code=303)
