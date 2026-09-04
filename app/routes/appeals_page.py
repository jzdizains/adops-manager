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
    return render(request, "appeals.html", {
        "title": "Appeals", "s": s, "summary": appeals_mod.summary(db),
        "open_rows": open_rows, "waiting": waiting, "history": history,
        "labels": appeals_mod.STATUS_LABELS, "preview": preview,
        "keywords": appeals_mod.skip_keywords(s),
    })


@router.post("/appeals/scan")
def scan_now(db: Session = Depends(get_db)):
    """Re-run the issue scan (which feeds the appeals engine) right now."""
    result = issues_mod.scan(db)
    return _back(ok=f"Scanned {result['accounts_scanned']} account(s) — rejections and appeal answers are up to date.")


@router.post("/appeals/refresh")
def refresh_now(db: Session = Depends(get_db)):
    n = appeals_mod.refresh(db, max_age_min=0)
    waiting = db.query(models.Appeal).filter(models.Appeal.status == "appealing").count()
    return _back(ok=f"Asked TikTok about {waiting} open appeal(s): {n} answered." if waiting
                 else "No appeals are waiting on TikTok.")


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
    text = reason.strip() or None
    if appeals_mod.file_appeal(db, row, token, filed_by="manual", reason=text):
        return _back(ok=f"Appeal filed for “{(row.ad_name or row.adgroup_id)[:50]}”. TikTok aims to answer within 24 hours.")
    return _back(err=f"TikTok refused the appeal: {row.error}")


@router.post("/appeals/file-all")
def file_all(db: Session = Depends(get_db)):
    """Appeal every open rejection (pending, skipped and errored ones alike —
    the operator confirmed on the page)."""
    s = get_settings(db)
    rows = (db.query(models.Appeal)
            .filter(models.Appeal.status.in_(("pending", "skipped", "error"))).all())
    ok = err = 0
    for row in rows:
        token = _token_for(db, row.advertiser_id)
        if not token:
            err += 1
            continue
        if appeals_mod.file_appeal(db, row, token, s, filed_by="manual"):
            ok += 1
        else:
            err += 1
    if not rows:
        return _back(ok="Nothing to appeal.")
    return _back(ok=f"Filed {ok} appeal(s)" + (f", {err} refused — see the rows" if err else "") + ".")


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
