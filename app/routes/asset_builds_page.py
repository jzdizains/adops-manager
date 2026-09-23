"""Instant Page / Instant Form build queue + form templates (v150). JSON for the pop-ups on
the Instant Pages and Lead Forms screens — nothing here renders a page of its own."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import asset_builds, models, scope as scope_mod
from ..database import get_db

router = APIRouter()
KINDS = ("page", "form")


def _template(db: Session, sc, kind: str, template_id):
    M = models.PageTemplate if kind == "page" else models.FormTemplate
    t = db.get(M, int(template_id)) if str(template_id or "").isdigit() else None
    return t if (t is not None and sc.owns(t)) else None


@router.get("/builds/targets.json")
def build_targets(request: Request, kind: str = "page", template_id: str = "", db: Session = Depends(get_db)):
    """Accounts grouped by Business Center, each marked 'has it' (a page / form of this name)."""
    sc = scope_mod.for_request(request, db)
    t = _template(db, sc, kind, template_id) if kind in KINDS else None
    if t is None:
        return JSONResponse({"ok": False, "error": "That template isn't in your workspace."}, status_code=404)
    return JSONResponse({"ok": True, "name": t.name, **asset_builds.targets(db, models, sc, kind, t.name)})


@router.post("/builds/queue")
async def build_queue(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    kind = str(f.get("kind") or "")
    sc = scope_mod.for_request(request, db)
    t = _template(db, sc, kind, f.get("template_id")) if kind in KINDS else None
    if t is None:
        return JSONResponse({"ok": False, "error": "That template isn't in your workspace."}, status_code=404)
    if kind == "form" and not t.master_form_id:
        return JSONResponse({"ok": False, "error": "Pick the master form this template copies first (Edit the template)."})
    ids = [x.strip() for x in str(f.get("advertiser_ids") or "").split(",") if x.strip()]
    if not ids:
        return JSONResponse({"ok": False, "error": "Pick at least one account."})
    r = asset_builds.queue(db, models, sc, kind, t, ids)
    what = "page" if kind == "page" else "form"
    msg = f"Queued “{t.name}” on {r['queued']} account(s)." if r["queued"] else f"Nothing to build — every picked account already has the {what} or it's queued."
    if r["queued"] and (r["skipped_have"] or r["skipped_busy"]):
        msg += f" Skipped {r['skipped_have'] + r['skipped_busy']} that already have it or are queued."
    from .. import audit
    audit.from_request(db, models, request, f"{kind}.build_queued", target=t.name, detail=f"{r['queued']} account(s)")
    return JSONResponse({"ok": True, "message": msg, **r})


@router.get("/builds.json")
def builds_json(request: Request, kind: str = "page", db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    return JSONResponse({"ok": True, **asset_builds.recent(db, models, sc, kind if kind in KINDS else "page")})


def _row(db: Session, request: Request, build_id: int):
    sc = scope_mod.for_request(request, db)
    r = db.get(models.AssetBuild, build_id)
    return r if (r is not None and sc.allows(r.advertiser_id)) else None


@router.post("/builds/{build_id}/retry")
def build_retry(build_id: int, request: Request, db: Session = Depends(get_db)):
    r = _row(db, request, build_id)
    if r is None or not asset_builds.retry(db, models, r):
        return JSONResponse({"ok": False, "error": "Only a failed or cancelled build can be retried."})
    return JSONResponse({"ok": True})


@router.post("/builds/{build_id}/cancel")
def build_cancel(build_id: int, request: Request, db: Session = Depends(get_db)):
    r = _row(db, request, build_id)
    if r is None or not asset_builds.cancel(db, models, r):
        return JSONResponse({"ok": False, "error": "Only a build still waiting can be cancelled (a running one finishes)."})
    return JSONResponse({"ok": True})


@router.post("/builds/batch/{batch}/retry-failed")
def batch_retry(batch: str, request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    n = 0
    for r in db.query(models.AssetBuild).filter(models.AssetBuild.batch == batch, models.AssetBuild.status == "failed"):
        if sc.allows(r.advertiser_id):
            r.status, r.step, r.error, r.step_at, r.started_at, r.finished_at = "pending", "Waiting in the queue", "", None, None, None
            n += 1
    db.commit()
    if n:
        asset_builds.kick(db)
    return JSONResponse({"ok": True, "n": n})


# ---------------------------------------------------------------------------------------
# form templates ("offers" for Instant Forms)

FORM_FIELDS = (("question_label", 300), ("question_options", 2000), ("company_name", 120), ("privacy_url", 2000),
               ("thanks_title", 120), ("thanks_description", 500), ("cta_title", 40), ("destination_url", 2000))


@router.post("/lead-forms/templates/save")
async def form_template_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    sc = scope_mod.for_request(request, db)
    name = str(f.get("name") or "").strip()[:100]
    if not name:
        return JSONResponse({"ok": False, "error": "Give the template a name — it becomes the form's name on every account."})
    dest, priv = str(f.get("destination_url") or "").strip(), str(f.get("privacy_url") or "").strip()
    for label, u in (("offer link", dest), ("privacy link", priv)):
        if u and not u.startswith(("http://", "https://")):
            return JSONResponse({"ok": False, "error": f"The {label} must start with http:// or https://"})
    master = str(f.get("master_form_id") or "").strip()
    owner = ""
    if master:
        mrow = db.query(models.LeadForm).filter_by(form_id=master).first()
        if mrow is None or not sc.allows(mrow.owner_advertiser_id):
            return JSONResponse({"ok": False, "error": "That master form isn't in your workspace — Sync, then pick it again."})
        owner = mrow.owner_advertiser_id
    t = _template(db, sc, "form", f.get("id")) if str(f.get("id") or "").isdigit() else None
    if t is None:
        t = models.FormTemplate(owner_user_id=sc.owner_for_new, name=name)
        db.add(t)
    t.name = name
    for k, cap in FORM_FIELDS:
        setattr(t, k, str(f.get(k) or "").strip()[:cap])
    t.master_form_id, t.master_advertiser_id = master, owner
    t.updated_at = __import__("datetime").datetime.utcnow()
    db.commit()
    return JSONResponse({"ok": True, "id": t.id, "message": f"Template “{t.name}” saved. Forms already built from it are not changed."})


@router.post("/lead-forms/templates/{template_id}/delete")
def form_template_delete(template_id: int, request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    t = _template(db, sc, "form", template_id)
    if t is None:
        return JSONResponse({"ok": False, "error": "That template is already gone."})
    db.delete(t)
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/lead-forms/master.json")
def form_master_fields(request: Request, form_id: str = "", db: Session = Depends(get_db)):
    """What a master form says now (to prefill a new template and show its answer count)."""
    from .. import instant_page_web as web, lead_form_builder, spark_web_api
    sc = scope_mod.for_request(request, db)
    row = db.query(models.LeadForm).filter_by(form_id=form_id).first()
    if row is None or not sc.allows(row.owner_advertiser_id):
        return JSONResponse({"ok": False, "error": "Pick a form in your workspace."}, status_code=404)
    if not spark_web_api.load_cookies():
        return JSONResponse({"ok": False, "error": "Needs the ads.tiktok.com cookies (TikTok Cookies page)."})
    try:
        info, _shape, _p = web.read_page(form_id, row.owner_advertiser_id, row.owner_advertiser_id)
    except spark_web_api.WebAuthError as e:
        return JSONResponse({"ok": False, "error": f"The web session is dead: {str(e)[:120]}"})
    pi = ((info.get("data") or {}).get("page_info") or {}) if isinstance(info.get("data"), dict) else {}
    raw = pi.get("publish_data") or pi.get("data")
    if not raw:
        return JSONResponse({"ok": False, "error": "TikTok returned no definition for that form."})
    return JSONResponse({"ok": True, "fields": lead_form_builder.extract_form_fields(raw, row.name)})
