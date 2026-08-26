"""/automation — the audit trail of everything the rule engine did, with undo.
Also /queue — launch queue statuses with retry/cancel."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/automation")
def automation_page(request: Request, db: Session = Depends(get_db)):
    actions = (db.query(models.RuleAction)
               .order_by(models.RuleAction.created_at.desc()).limit(200).all())
    topups = (db.query(models.TopUp)
              .order_by(models.TopUp.created_at.desc()).limit(100).all())
    # which paused campaigns are still paused (offer Resume)
    paused_ids = {r.campaign_id for r in
                  db.query(models.CampaignRecord)
                  .filter(models.CampaignRecord.operation_status == "DISABLE")}
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount).all()}
    still_paused_count = sum(
        1 for a in actions
        if a.action == "pause" and a.ok and a.campaign_id in paused_ids)
    return render(request, "automation.html", {
        "title": "Automation", "actions": actions, "topups": topups,
        "paused_ids": paused_ids, "names": names,
        "still_paused_count": still_paused_count,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


def _resume(db: Session, advertiser_id: str, campaign_id: str) -> str | None:
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not acct or not acct.access_token:
        return "no token for that account"
    try:
        tiktok_api.update_campaign_status(acct.access_token, advertiser_id,
                                          [campaign_id], "ENABLE")
    except tiktok_api.TikTokError as e:
        return f"TikTok refused (code {e.code})"
    rec = (db.query(models.CampaignRecord)
           .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
    if rec:
        rec.operation_status = "ENABLE"
    db.add(models.RuleAction(advertiser_id=advertiser_id, campaign_id=campaign_id,
                             campaign_name=rec.campaign_name if rec else "",
                             rule="manual resume", action="resume", ok=True,
                             detail="resumed by operator from Automation page"))
    db.commit()
    return None


@router.post("/automation/resume")
def resume_one(advertiser_id: str = Form(...), campaign_id: str = Form(...),
               db: Session = Depends(get_db)):
    err = _resume(db, advertiser_id, campaign_id)
    if err:
        return RedirectResponse(f"/automation?err={err[:150]}", status_code=303)
    return RedirectResponse("/automation?ok=Campaign+resumed", status_code=303)


@router.post("/automation/resume-all")
def resume_all(db: Session = Depends(get_db)):
    """Resume every campaign the rule engine paused that is still paused."""
    paused_ids = {r.campaign_id: r.advertiser_id for r in
                  db.query(models.CampaignRecord)
                  .filter(models.CampaignRecord.operation_status == "DISABLE")}
    engine_paused = (db.query(models.RuleAction)
                     .filter(models.RuleAction.action == "pause",
                             models.RuleAction.ok == True).all())  # noqa: E712
    done, failed = 0, 0
    seen = set()
    for a in engine_paused:
        if a.campaign_id in seen or a.campaign_id not in paused_ids:
            continue
        seen.add(a.campaign_id)
        if _resume(db, paused_ids[a.campaign_id], a.campaign_id) is None:
            done += 1
        else:
            failed += 1
    msg = f"Resumed+{done}+campaign(s)"
    if failed:
        msg += f"&err={failed}+failed"
    return RedirectResponse(f"/automation?ok={msg}", status_code=303)


# ---------------------------------------------------------------------------
# Launch queue
# ---------------------------------------------------------------------------

@router.get("/queue")
def queue_page(request: Request, db: Session = Depends(get_db)):
    items = (db.query(models.LaunchQueueItem)
             .order_by(models.LaunchQueueItem.created_at.desc()).limit(200).all())
    templates = {t.id: t.name for t in db.query(models.Template).all()}
    sparks = {s.id: (s.name or s.code[:14]) for s in db.query(models.SparkCode).all()}
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount).all()}
    pending = sum(1 for i in items if i.status == "pending")
    return render(request, "queue.html", {
        "title": "Launch Queue", "items": items, "templates": templates,
        "sparks": sparks, "names": names, "pending": pending,
        "ok": request.query_params.get("ok", ""),
    })


@router.post("/queue/{item_id}/retry")
def retry_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(models.LaunchQueueItem, item_id)
    if item and item.status == "failed":
        item.status = "pending"
        item.attempts = 0
        db.commit()
    return RedirectResponse("/queue?ok=requeued", status_code=303)


@router.post("/queue/{item_id}/cancel")
def cancel_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(models.LaunchQueueItem, item_id)
    if item and item.status in ("pending", "failed"):
        db.delete(item)
        db.commit()
    return RedirectResponse("/queue?ok=removed", status_code=303)


@router.post("/queue/process-now")
def process_now(db: Session = Depends(get_db)):
    """Manual kick — process a batch immediately instead of waiting a sweep."""
    from .. import queue_worker
    n = queue_worker.process(db)
    return RedirectResponse(f"/queue?ok=processed+{n}+item(s)", status_code=303)
