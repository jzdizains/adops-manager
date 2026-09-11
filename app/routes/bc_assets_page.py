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

from .. import bc_assets, jobs, queries
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
        rows = [r for r in accounts
                if not r.get("in_main_bc") or not r.get("pixels") or r.get("profiles_missing")]
    elif show == "disabled":
        rows = [r for r in accounts if not r.get("enabled")]
    else:
        rows = accounts
    return render(request, "bc_assets.html", {
        "title": "Assets", "active": "settings", "snap": snap, "rows": rows, "show": show,
        "n_all": len(accounts),
        "n_gaps": sum(1 for r in accounts
                      if not r.get("in_main_bc") or not r.get("pixels") or r.get("profiles_missing")),
        "main_bc": snap.get("main_bc") or bc_assets.main_bc_id(db),
        "running": bool(jobs.pending(db, "bc_assets_scan")),
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


@router.post("/bc-assets/main")
def bc_assets_main(bc_id: str = Form(""), db: Session = Depends(get_db)):
    """Which BC owns the pixel and the profiles (everything else is measured against it)."""
    clean = "".join(ch for ch in (bc_id or "") if ch.isdigit())
    queries.upsert_setting(db, bc_assets.MAIN_BC_KEY, clean)
    db.commit()
    return _back(ok=f"Main Business Center set to {clean}. Run the audit again to refresh."
                 if clean else "Main Business Center cleared.")
