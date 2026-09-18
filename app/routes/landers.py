"""Landers (v124) — build honest pages for your own domains from templates, with live
settings and a per-page funnel. See app/landers.py for the builder."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import landers as kit, models, scope as scope_mod
from ..database import get_db
from ..templating import render
from . import guard

router = APIRouter()

PUBLIC_CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


def _track_host(db: Session) -> str:
    from .. import settings_store, tracking
    s = settings_store.for_view(db)
    return (tracking.base_url(db, s) or "").rstrip("/")


def _extra_params(db: Session) -> str:
    from .. import settings_store
    return settings_store.for_view(db).get("url_param_extra", "") or ""


def funnel(db: Session, slugs: list[str], days: int = 7) -> dict[str, dict]:
    """slug → {step: distinct visitors, step_inapp: of which still in an app, views_browser: …}."""
    if not slugs:
        return {}
    since = datetime.utcnow() - timedelta(days=days)
    q = (db.query(models.LanderEvent.page, models.LanderEvent.step, models.LanderEvent.inapp != "",
                  func.count(func.distinct(models.LanderEvent.vid)))
         .filter(models.LanderEvent.page.in_(slugs), models.LanderEvent.created_at >= since)
         .group_by(models.LanderEvent.page, models.LanderEvent.step, models.LanderEvent.inapp != ""))
    out: dict[str, dict] = {s: {} for s in slugs}
    for page, step, inapp, n in q:
        d = out.setdefault(page, {})
        d[step] = d.get(step, 0) + int(n or 0)
        if inapp:
            d[step + "_inapp"] = d.get(step + "_inapp", 0) + int(n or 0)
    return out


@router.get("/landers")
def page(request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    rows = sc.owned(db.query(models.Lander), models.Lander).order_by(models.Lander.updated_at.desc()).all()
    stats = funnel(db, [r.slug for r in rows])
    host = _track_host(db)
    items = []
    for r in rows:
        cfg = kit.config_of(r)
        items.append({"id": r.id, "slug": r.slug, "name": r.name or r.slug, "template": r.template,
                      "template_label": (kit.TEMPLATES.get(r.template) or {}).get("label", r.template),
                      "domain": r.domain or "", "enabled": bool(r.enabled), "next": cfg.get("next", ""), "rules": len(cfg.get("rules") or []),
                      "escape": cfg.get("escape", {}), "pixel": cfg.get("pixel", ""), "updated": r.updated_at,
                      "stats": stats.get(r.slug, {}), "cfg": cfg})
    return render(request, "landers.html", {
        "title": "Landers", "items": items, "items_json": json.dumps(items, default=str), "track_host": host,
        "templates": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "file"}} for k, v in kit.TEMPLATES.items()],
        "escape_android": kit.ESCAPE_ANDROID, "escape_ios": kit.ESCAPE_IOS, "pixel_events": kit.PIXEL_EVENTS, "age_brackets": kit.AGE_BRACKETS,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/landers/save")
async def save(request: Request, db: Session = Depends(get_db)):
    """Create or update one lander (the editor drawer posts JSON-ish form fields; fetch gets JSON back)."""
    sc = scope_mod.for_request(request, db)
    form = await request.form()
    wants_json = request.headers.get("x-requested-with") == "fetch"

    def fail(msg: str, status: int = 400):
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=status)
        return RedirectResponse("/landers?err=" + quote(msg), status_code=303)

    lid = str(form.get("id") or "")
    slug = str(form.get("slug") or "").strip().lower()
    if not kit.valid_slug(slug):
        return fail("Slug: 2–40 characters, a–z, 0–9 and dashes, starting with a letter or digit.")
    template = str(form.get("template") or "prelander")
    if template not in kit.TEMPLATES:
        return fail("Unknown template.")
    raw = {k: form.get(k) for k in form.keys()}
    cfg, problems = kit.clean_config(template, raw)
    if problems:
        return fail(" ".join(problems))
    row = db.get(models.Lander, int(lid)) if lid.isdigit() else None
    if row is not None and not sc.owns(row):
        return fail("Not yours.", 404)
    clash = db.query(models.Lander).filter(models.Lander.slug == slug).first()
    if clash is not None and (row is None or clash.id != row.id):
        return fail("That slug is taken.")
    if row is None:
        row = models.Lander(owner_user_id=sc.owner_for_new, slug=slug)
        db.add(row)
    row.slug = slug
    row.name = str(form.get("name") or "")[:120].strip() or slug
    row.template = template
    row.domain = str(form.get("domain") or "")[:200].strip()
    row.enabled = form.get("enabled") not in (None, "", "0", "off")
    row.config = json.dumps(cfg)
    db.commit()
    if wants_json:
        return JSONResponse({"ok": True, "id": row.id, "slug": row.slug})
    return RedirectResponse(f"/landers?ok=saved#{row.slug}", status_code=303)


@router.post("/landers/{lander_id}/delete")
def delete(lander_id: int, db: Session = Depends(get_db), sc: scope_mod.Scope = Depends(guard.view)):
    row = db.get(models.Lander, lander_id)
    if row is not None and sc.owns(row):
        db.delete(row)
        db.commit()
    return RedirectResponse("/landers?ok=deleted", status_code=303)


@router.get("/landers/{lander_id}/preview")
def preview(lander_id: int, request: Request, db: Session = Depends(get_db), sc: scope_mod.Scope = Depends(guard.view)):
    """The built page, as the visitor would get it (behind the login; beacons are real, so the
    preview counts as a visit of its own — the funnel shows it under the slug)."""
    row = db.get(models.Lander, lander_id)
    if row is None or not sc.owns(row):
        return Response("Not found", status_code=404)
    html = kit.build_html(row, _track_host(db), _extra_params(db))
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN"})


@router.get("/landers/{lander_id}/package.zip")
def package(lander_id: int, db: Session = Depends(get_db), sc: scope_mod.Scope = Depends(guard.view)):
    row = db.get(models.Lander, lander_id)
    if row is None or not sc.owns(row):
        return Response("Not found", status_code=404)
    data = kit.package(row, _track_host(db), _extra_params(db))
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="lander-{row.slug}.zip"', "Cache-Control": "no-store"})


# ---- public: the live settings a built page re-reads at open --------------------------------

@router.get("/t/l/{slug}.json")
def live_config(slug: str, request: Request, db: Session = Depends(get_db)):
    """Only what the page needs to route (next, rules, escape method, pixel event) — never
    owner ids, never the texts (those are baked). Never cached (country is per visitor), CORS open: the page
    lives on another domain. Country comes from the edge header when the host sets one."""
    if not kit.valid_slug(slug):
        return JSONResponse({"ok": False}, status_code=404, headers=PUBLIC_CORS)
    row = db.query(models.Lander).filter(models.Lander.slug == slug).first()
    if row is None or not row.enabled:
        return JSONResponse({"ok": False}, status_code=404, headers=PUBLIC_CORS)
    country = (request.headers.get("cf-ipcountry") or request.headers.get("x-country") or request.headers.get("x-vercel-ip-country") or "").upper()[:2]
    return JSONResponse({"ok": True, "cfg": kit.public_config(row), "country": country if country not in ("XX", "T1") else ""},
                        headers=PUBLIC_CORS)
