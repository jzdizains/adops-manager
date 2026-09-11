"""/bc-assets — read-only audit of the asset wiring.

Shows, from the last scan: the Business Centers the stored tokens can see, what the
main BC owns (pixel + TikTok profiles), and per ad account whether it is in the main
BC, has the pixel, and has every profile. Nothing here writes to TikTok — the scan is
a background job so a page view never makes dozens of API calls.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import bc_assets, config, jobs, models, queries
from ..database import get_db
from ..templating import render

router = APIRouter()


def _back(ok: str = "", err: str = "") -> RedirectResponse:
    q = f"?ok={quote(ok)}" if ok else (f"?err={quote(err)}" if err else "")
    return RedirectResponse("/bc-assets" + q, status_code=303)


@router.get("/bc-assets")
def bc_assets_page(request: Request, db: Session = Depends(get_db)):
    snap = bc_assets.snapshot(db)
    accounts = snap.get("accounts") or []
    show = request.query_params.get("show", "gaps")
    if show == "gaps":
        # a pixel the audit could not READ is not a gap — it is an unknown, and listing
        # all 428 accounts as "needs work" because of one refused call is noise, not signal
        rows = [r for r in accounts
                if not r.get("in_main_bc")
                or (not r.get("pixels") and not r.get("pixels_unknown"))
                or r.get("profiles_missing")]
    elif show == "disabled":
        rows = [r for r in accounts if not r.get("enabled")]
    else:
        rows = accounts
    # Business Centers present in the table, for the filter — name them from the audit so a BC
    # the dashboard can no longer see still filters to the accounts it owns
    bc_names: dict[str, str] = {}
    for r in accounts:
        key = r.get("owner_bc") or ""
        bc_names.setdefault(key, r.get("owner_bc_name") or key or "No Business Center")
    bc_filter = sorted(({"id": k, "name": v,
                         "n": sum(1 for r in accounts if (r.get("owner_bc") or "") == k)}
                        for k, v in bc_names.items()),
                       key=lambda b: (b["id"] == "", b["name"].lower()))
    return render(request, "bc_assets.html", {
        "title": "Assets", "active": "settings", "snap": snap, "rows": rows, "show": show,
        "bc_filter": bc_filter,
        "n_all": len(accounts),
        "n_gaps": sum(1 for r in accounts
                      if not r.get("in_main_bc")
                      or (not r.get("pixels") and not r.get("pixels_unknown"))
                      or r.get("profiles_missing")),
        "main_bc": snap.get("main_bc") or bc_assets.main_bc_id(db),
        "running": bool(jobs.pending(db, "bc_assets_scan")),
        "wiring": bool(jobs.pending(db, "bc_assets_wire")),
        "wire": bc_assets.last_wire(db),
        "roles": ("OPERATOR", "ADMIN", "ANALYST"),
        "watch": [dict(w, **bc_assets.stage(db, w["bc_id"], snap)) for w in bc_assets.watchlist(db)],
        "owner_email": getattr(getattr(request.state, "user", None), "email", "") or config.OWNER_EMAIL,
        "connecting": bool(jobs.pending(db, "bc_assets_connect")),
    })


@router.get("/bc-assets/state.json")
def bc_assets_state(db: Session = Depends(get_db)):
    """Tiny poll for the page: is something running, what is it doing, and has the audit
    changed since the page was drawn. No TikTok calls — one job row and one setting."""
    from fastapi.responses import JSONResponse
    job = (db.query(models.Job)
           .filter(models.Job.kind.in_(("bc_assets_scan", "bc_assets_wire", "bc_assets_connect")))
           .order_by(models.Job.id.desc()).first())
    return JSONResponse({
        "ok": True,
        "at": bc_assets.snapshot_at(db),     # one short setting — never parse the whole snapshot here,
                                             # this runs every couple of seconds while a job is going
        "running": bool(job and job.status in ("queued", "running")),
        "kind": job.kind if job else "",
        "title": (job.title or "") if job else "",
        "status": (job.status or "") if job else "",
        "progress": (job.progress or "") if job else "",
        "detail": (job.detail or "") if job else "",
    })


@router.post("/bc-assets/scan")
def bc_assets_scan(db: Session = Depends(get_db)):
    """Queue the read-only scan (slow lane — it makes one call per pixel and profile)."""
    if not queries.any_access_token(db):
        return _back(err="Connect TikTok first — the audit reads through an ad account's token.")
    job, created = jobs.enqueue_once(db, "bc_assets_scan", "Audit pixel + profile links", {},
                                     href="/bc-assets")
    return _back(ok="Reading the Business Centers in the background — watch it on the Jobs page."
                 if created else f"A scan is already {job.status}.")


@router.post("/bc-assets/wire")
def bc_assets_wire(advertiser_id: str = Form(""), role: str = Form("OPERATOR"),
                   mode: str = Form(""), db: Session = Depends(get_db)):
    """Give ONE ad account the pixel and every profile. `preview` sends nothing —
    it only records the exact requests that a `send` would make."""
    adv = "".join(ch for ch in (advertiser_id or "") if ch.isdigit())
    if not adv:
        return _back(err="Give the ad account id first.")
    m = bc_assets.parse_mode(mode)
    if m is None:
        return _back(err="Use the Preview or Wire button — the browser didn't say which one you pressed, "
                         "and nothing is ever sent on a guess.")
    if not queries.any_access_token(db):
        return _back(err="Connect TikTok first.")
    job, created = jobs.enqueue_once(db, "bc_assets_wire",
                                     f"{'PREVIEW (nothing sent)' if m == 'preview' else 'Wire'} assets for {adv}",
                                     {"advertiser_id": adv, "role": role, "dry_run": m == "preview"},
                                     href="/bc-assets")
    if not created:
        return _back(ok=f"A wiring run is already {job.status}.")
    return _back(ok=("Previewing — nothing is sent; the exact requests appear here in a moment."
                     if m == "preview" else "Sending to TikTok — the answers appear here in a moment."))


@router.post("/bc-assets/bc/add")
def bc_assets_bc_add(bc_id: str = Form(""), label: str = Form(""), db: Session = Depends(get_db)):
    """Put a satellite BC on the list. A brand-new one isn't visible to the API yet — the
    list is how the dashboard tracks it through invite → accept → connect."""
    clean = "".join(ch for ch in (bc_id or "") if ch.isdigit())
    if not clean:
        return _back(err="A Business Center ID is a number — copy it from that BC's Settings.")
    bc_assets.watch_add(db, clean, label)
    return _back(ok=f"Added {clean}. Next: invite your email into it as Admin, accept, then Re-check.")


@router.post("/bc-assets/bc/remove")
def bc_assets_bc_remove(bc_id: str = Form(""), db: Session = Depends(get_db)):
    bc_assets.watch_remove(db, "".join(ch for ch in (bc_id or "") if ch.isdigit()))
    return _back(ok="Removed from the list (nothing on TikTok was changed).")


@router.post("/bc-assets/invite")
def bc_assets_invite(bc_id: str = Form(""), email: str = Form(""), role: str = Form("ADMIN"),
                     db: Session = Depends(get_db)):
    """Invite an email into a BC — only works where a stored token is already Admin there."""
    rep = bc_assets.invite(db, "".join(ch for ch in (bc_id or "") if ch.isdigit()), email.strip(),
                           "ADMIN" if role != "STANDARD" else "STANDARD")
    return _back(err=rep["error"]) if rep.get("error") else _back(ok=rep.get("summary", "Invitation sent."))


@router.post("/bc-assets/connect")
def bc_assets_connect(bc_id: str = Form(""), role: str = Form("OPERATOR"), mode: str = Form(""),
                      email: str = Form(""), db: Session = Depends(get_db)):
    """One Business Center, one button: share all its ad accounts in, then pixel + profiles."""
    clean = "".join(ch for ch in (bc_id or "") if ch.isdigit())
    if not clean:
        return _back(err="Which Business Center?")
    m = bc_assets.parse_mode(mode)
    if m is None:
        return _back(err="Use the Preview or Connect button — the browser didn't say which one you pressed, "
                         "and nothing is ever sent on a guess.")
    job, created = jobs.enqueue_once(db, "bc_assets_connect",
                                     f"{'PREVIEW (nothing sent)' if m == 'preview' else 'Connect'} Business Center {clean}",
                                     {"bc_id": clean, "role": role, "dry_run": m == "preview", "email": email},
                                     href="/bc-assets")
    if not created:
        return _back(ok=f"A run is already {job.status}.")
    return _back(ok="Previewing — nothing is sent." if m == "preview"
                 else "Connecting in the background — the report appears here when it finishes.")


@router.post("/bc-assets/pixels")
def bc_assets_pixels(pixel_ids: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    """Which of the main BC's pixels this flow uses. Nothing selected = all of them."""
    ids = [v for v in pixel_ids if str(v).strip()]
    bc_assets.set_chosen_pixels(db, ids)
    if not ids:
        return _back(ok="Using every pixel the main Business Center owns. Run the audit to refresh.")
    return _back(ok=f"Using {len(ids)} pixel(s). Only these are read and linked from now on — "
                    "run the audit to refresh.")


@router.post("/bc-assets/main")
def bc_assets_main(bc_id: str = Form(""), db: Session = Depends(get_db)):
    """Which BC owns the pixel and the profiles (everything else is measured against it)."""
    clean = "".join(ch for ch in (bc_id or "") if ch.isdigit())
    queries.upsert_setting(db, bc_assets.MAIN_BC_KEY, clean)
    db.commit()
    return _back(ok=f"Main Business Center set to {clean}. Run the audit again to refresh."
                 if clean else "Main Business Center cleared.")
