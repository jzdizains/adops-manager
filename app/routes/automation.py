"""/automation — the audit trail of everything the rule engine did, with undo.
Also /queue — launch queue statuses with retry/cancel."""
from __future__ import annotations

from datetime import datetime, timezone  # noqa: F401
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/automation")
def automation_page(request: Request):
    """Merged into Health → Automation; keep the old URL working."""
    return RedirectResponse("/monitor?view=automation", status_code=303)


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
def resume_one(request: Request, advertiser_id: str = Form(...), campaign_id: str = Form(...),
               db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    if not scope_mod.for_request(request, db).allows(advertiser_id):
        return RedirectResponse("/monitor?view=automation&err=That+campaign+isn't+in+your+workspace", status_code=303)
    err = _resume(db, advertiser_id, campaign_id)
    if err:
        return RedirectResponse(f"/monitor?view=automation&err={err[:150]}", status_code=303)
    return RedirectResponse("/monitor?view=automation&ok=Campaign+resumed", status_code=303)


@router.post("/automation/pause")
def pause_one(request: Request, advertiser_id: str = Form(...), campaign_id: str = Form(...), rule: str = Form(""),
              db: Session = Depends(get_db)):
    """A flagged / dry-run / held rule action, paused by hand."""
    from .. import scope as scope_mod
    if not scope_mod.for_request(request, db).allows(advertiser_id):
        return RedirectResponse("/monitor?view=automation&err=That+campaign+isn't+in+your+workspace", status_code=303)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not acct or not acct.access_token:
        return RedirectResponse("/monitor?view=automation&err=No+token+for+that+account", status_code=303)
    try:
        tiktok_api.update_campaign_status(acct.access_token, advertiser_id, [campaign_id], "DISABLE")
    except tiktok_api.TikTokError as e:
        return RedirectResponse(f"/monitor?view=automation&err=TikTok+refused+(code+{e.code})", status_code=303)
    rec = db.query(models.CampaignRecord).filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first()
    if rec:
        rec.operation_status = "DISABLE"
    db.add(models.RuleAction(advertiser_id=advertiser_id, campaign_id=campaign_id, campaign_name=rec.campaign_name if rec else "",
                             rule=(rule or "flagged")[:120], action="pause", ok=True, detail="paused by operator from a rule flag"))
    db.commit()
    return RedirectResponse("/monitor?view=automation&ok=Campaign+paused", status_code=303)


@router.post("/automation/resume-all")
def resume_all(request: Request, db: Session = Depends(get_db)):
    """Resume every campaign the rule engine paused that is still paused — in the workspace
    in view only (a buyer's "Resume all" must never turn on another buyer's campaigns)."""
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    paused_ids = {r.campaign_id: r.advertiser_id for r in
                  db.query(models.CampaignRecord)
                  .filter(models.CampaignRecord.operation_status == "DISABLE")
                  if sc.allows(r.advertiser_id)}
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
    return RedirectResponse(f"/monitor?view=automation&ok={msg}", status_code=303)


# ---------------------------------------------------------------------------
# Launch queue
# ---------------------------------------------------------------------------

@router.get("/queue")
def queue_page(request: Request, db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    items = [i for i in (db.query(models.LaunchQueueItem)
                         .order_by(models.LaunchQueueItem.created_at.desc()).limit(400).all())
             if sc.everything or i.launched_by == sc.user_id or sc.allows(i.advertiser_id)][:200]
    templates = {t.id: t.name for t in db.query(models.Template).all()}
    sparks = {s.id: (s.name or s.code[:14]) for s in db.query(models.SparkCode).all()}
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount).all()}
    pending = sum(1 for i in items if i.status == "pending")
    failed_n = sum(1 for i in items if i.status == "failed")
    return render(request, "queue.html", {
        "title": "Launch Queue", "items": items, "templates": templates,
        "sparks": sparks, "names": names, "pending": pending, "failed_n": failed_n,
        "ok": request.query_params.get("ok", ""),
    })


def _queue_visible(sc, item) -> bool:
    """A user may only touch a queue item they launched, or one on an account in their view."""
    return bool(sc.everything or item.launched_by == sc.user_id or sc.allows(item.advertiser_id))


@router.post("/queue/{item_id}/retry")
def retry_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    from .. import queue_worker
    item = db.get(models.LaunchQueueItem, item_id)
    if item and _queue_visible(sc, item) and item.status == "failed":
        if not queue_worker.retry_safe(db, item):
            return RedirectResponse("/queue?err=" + quote("Not re-queued: that launch may have left a campaign on the account — "
                                                          "check its launch result (Retry failed there continues inside the campaign)."), status_code=303)
        item.status = "pending"
        item.attempts = 0
        db.commit()
    return RedirectResponse("/queue?ok=requeued", status_code=303)


@router.post("/queue/retry-failed")
def retry_failed(request: Request, db: Session = Depends(get_db)):
    """Re-queue every failed item at once — but only the ones in THIS user's view (each
    gets a fresh attempt counter). One buyer's click must never relaunch another's failures."""
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    from .. import queue_worker
    n = skipped = 0
    for item in db.query(models.LaunchQueueItem).filter_by(status="failed"):
        if not _queue_visible(sc, item):
            continue
        if not queue_worker.retry_safe(db, item):
            skipped += 1                 # may have left a campaign behind — never a blind second one
            continue
        item.status = "pending"
        item.attempts = 0
        n += 1
    db.commit()
    tail = f"+·+{skipped}+left+alone+(they+may+have+made+a+campaign+—+check+their+launch+results)" if skipped else ""
    return RedirectResponse(f"/queue?ok={n}+launch(es)+re-queued{tail}", status_code=303)


@router.post("/queue/{item_id}/cancel")
def cancel_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    item = db.get(models.LaunchQueueItem, item_id)
    if item and _queue_visible(sc, item) and item.status in ("pending", "failed"):
        db.delete(item)
        db.commit()
    return RedirectResponse("/queue?ok=removed", status_code=303)


@router.post("/queue/process-now")
def process_now(db: Session = Depends(get_db)):
    """Manual kick — process a batch now instead of waiting a sweep. Runs in its own thread
    (a launch takes minutes; the request never waits) and never overlaps the sweep's pass."""
    import threading
    from .. import queue_worker
    from ..database import SessionLocal

    def _go():
        d = SessionLocal()
        try:
            queue_worker.process(d)
        except Exception:      # noqa: BLE001
            d.rollback()
        finally:
            d.close()
    threading.Thread(target=_go, name="queue-now", daemon=True).start()
    return RedirectResponse("/queue?ok=Processing+now+in+the+background+—+this+page+shows+each+item+as+it+finishes.", status_code=303)
