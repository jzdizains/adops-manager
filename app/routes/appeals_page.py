"""/appeals — every TikTok ad-review rejection and what happened to its appeal.

Automatic filing lives in app/appeals.py (runs inside the issue scan). This
page is the operator's view + manual controls: appeal one, appeal all open,
check TikTok for answers, dismiss a rejection that was fixed another way."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import appeals as appeals_mod
from .. import issues as issues_mod
from .. import models
from ..database import get_db
from ..settings_store import get_settings
from ..templating import render

router = APIRouter()


def _token_for(db: Session, advertiser_id: str) -> str:
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    return (acct.access_token if acct else "") or ""


def _back(ok: str = "", err: str = "") -> RedirectResponse:
    q = f"?ok={quote(ok)}" if ok else (f"?err={quote(err)}" if err else "")
    return RedirectResponse("/appeals" + q, status_code=303)


@router.get("/appeals")
def appeals_page(request: Request, db: Session = Depends(get_db)):
    s = get_settings(db)
    rows = db.query(models.Appeal).order_by(models.Appeal.created_at.desc()).limit(500).all()
    open_rows = [r for r in rows if r.status in ("pending", "skipped", "error")]
    waiting = [r for r in rows if r.status == "appealing"]
    history = [r for r in rows if r.status in appeals_mod.FINAL]
    preview = appeals_mod.render_reason(
        s.get("appeal_reason", ""), ad_name="My ad", campaign_name="MyCampaign_1",
        reasons="The ad or video has no background audio")
    # when each campaign was created (TikTok create_time, synced into CampaignRecord)
    cids = {r.campaign_id for r in rows if r.campaign_id}
    created = {}
    if cids:
        for rec in db.query(models.CampaignRecord).filter(models.CampaignRecord.campaign_id.in_(cids)).all():
            if rec.launched_at:
                created[rec.campaign_id] = rec.launched_at
    return render(request, "appeals.html", {
        "title": "Appeals", "s": s, "summary": appeals_mod.summary(db), "created": created,
        "open_rows": open_rows, "waiting": waiting, "history": history,
        "labels": appeals_mod.STATUS_LABELS, "preview": preview,
        "keywords": appeals_mod.skip_keywords(s),
    })


@router.post("/appeals/scan")
def scan_now(db: Session = Depends(get_db)):
    """Queue the issue scan (which feeds the appeals engine)."""
    from .. import jobs
    jobs.enqueue(db, "issues_scan", "Scan every account for rejected ads", {}, href="/appeals")
    return _back(ok="Scanning in the background — you'll get a notification when it's done.")


@router.post("/appeals/refresh")
def refresh_now(db: Session = Depends(get_db)):
    from .. import jobs
    waiting = db.query(models.Appeal).filter(models.Appeal.status == "appealing").count()
    if not waiting:
        return _back(ok="No appeals are waiting on TikTok.")
    jobs.enqueue(db, "appeals_refresh", f"Check TikTok's answer on {waiting} open appeal(s)", {}, href="/appeals")
    return _back(ok="Checking in the background — you'll get a notification.")


@router.post("/appeals/{row_id}/file")
def file_one(row_id: int, reason: str = Form(""), db: Session = Depends(get_db)):
    row = db.get(models.Appeal, row_id)
    if not row:
        return _back(err="That rejection is no longer tracked.")
    if row.status == "appealing":
        return _back(err="An appeal is already on file for that ad group.")
    if row.status in ("successful", "done", "failed"):
        return _back(err="TikTok already answered an appeal for that rejection — only one appeal per rejection is allowed.")
    token = _token_for(db, row.advertiser_id)
    if not token:
        return _back(err=f"No TikTok token for account {row.advertiser_name or row.advertiser_id}.")
    from .. import jobs
    jobs.enqueue(db, "appeals_file", f"Appeal “{(row.ad_name or row.adgroup_id)[:50]}”",
                 {"ids": [row.id], "reason": reason.strip()}, href="/appeals")
    return _back(ok="Filing in the background — you'll get a notification with TikTok's answer.")


@router.post("/appeals/file-selected")
async def file_selected(request: Request, db: Session = Depends(get_db)):
    """Appeal only the ticked rejections (one per ad group), optional shared reason."""
    form = await request.form()
    ids = []
    for v in form.getlist("ids"):
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if not ids:
        return _back(err="Tick at least one ad group first.")
    from .. import jobs
    jobs.enqueue(db, "appeals_file", f"Appeal {len(ids)} selected ad group(s)",
                 {"ids": ids, "reason": str(form.get("reason") or "").strip()}, href="/appeals")
    return _back(ok=f"Filing {len(ids)} appeal(s) in the background — you'll get a notification.")


def file_rows(db: Session, ids: list[int], reason: str = "") -> dict:
    """The filing itself (runs in a job)."""
    s = get_settings(db)
    ok = err = skipped = 0
    for row in db.query(models.Appeal).filter(models.Appeal.id.in_(ids)).all():
        if row.status not in ("pending", "skipped", "error"):
            skipped += 1
            continue
        token = _token_for(db, row.advertiser_id)
        if not token:
            err += 1
            continue
        if appeals_mod.file_appeal(db, row, token, s, filed_by="manual", reason=(reason or None)):
            ok += 1
        else:
            err += 1
    msg = f"Filed {ok} appeal(s)" + (f", {err} refused — see the rows" if err else "") + (f", {skipped} already handled" if skipped else "") + "."
    return {"ok": ok > 0 and err == 0, "detail": msg}


@router.post("/appeals/file-all")
def file_all(db: Session = Depends(get_db)):
    """Appeal every open rejection (pending, skipped and errored ones alike —
    the operator confirmed on the page)."""
    from .. import jobs
    ids = [r.id for r in db.query(models.Appeal.id)
           .filter(models.Appeal.status.in_(("pending", "skipped", "error"))).all()]
    if not ids:
        return _back(ok="Nothing to appeal.")
    jobs.enqueue(db, "appeals_file", f"Appeal all {len(ids)} open ad group(s)", {"ids": ids, "reason": ""}, href="/appeals")
    return _back(ok=f"Filing {len(ids)} appeal(s) in the background — you'll get a notification.")


@router.post("/appeals/{row_id}/dismiss")
def dismiss(row_id: int, db: Session = Depends(get_db)):
    """Operator handled it another way (edited the ad, deleted it, or doesn't care)."""
    row = db.get(models.Appeal, row_id)
    if not row:
        return _back(err="That rejection is no longer tracked.")
    if row.status not in ("pending", "skipped", "error"):
        return _back(err="Only rejections without an appeal on file can be dismissed.")
    row.status = "dismissed"
    row.error = ""
    row.resolved_at = appeals_mod._now()
    db.commit()
    return _back(ok="Dismissed. It comes back only if TikTok reviews the ad group again and rejects it anew.")
