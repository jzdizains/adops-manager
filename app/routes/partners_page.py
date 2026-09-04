"""/partners — set up a new partner BC in one go (the API-supported steps),
track each step, and keep the website-only steps as a ticked checklist."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models, partners, queries, tiktok_api
from ..balances import bc_portal_url
from ..database import get_db
from ..templating import render

router = APIRouter()


def _back(ok: str = "", err: str = "", anchor: str = "") -> RedirectResponse:
    q = f"?ok={quote(ok)}" if ok else (f"?err={quote(err)}" if err else "")
    return RedirectResponse("/partners" + q + (f"#{anchor}" if anchor else ""), status_code=303)


def _clean_ids(raw: list[str]) -> str:
    seen, out = set(), []
    for v in raw:
        for part in str(v).replace("\n", ",").split(","):
            p = part.strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return ",".join(out)


@router.get("/partners")
def partners_page(request: Request, db: Session = Depends(get_db)):
    token = queries.any_access_token(db)
    roles = partners.bc_roles(token) if token else {}
    known = {bc.bc_id: bc for bc in db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()}
    bcs = []
    for bid in sorted(set(known) | set(roles), key=lambda b: (known[b].name if b in known else roles.get(b, {}).get("name", b)).lower()):
        r = roles.get(bid, {})
        bcs.append({"bc_id": bid, "name": (known[bid].name if bid in known else r.get("name")) or bid,
                    "user_role": r.get("user_role", ""), "admin": r.get("user_role", "").upper() == "ADMIN"})
    rows = db.query(models.PartnerSetup).order_by(models.PartnerSetup.created_at.desc()).limit(200).all()
    last_email = queries.get_setting(db, "partners_last_email", "")
    return render(request, "partners.html", {
        "title": "Partners", "bcs": bcs, "rows": rows, "has_token": bool(token),
        "last_email": last_email, "roles_loaded": bool(roles),
        "portal": bc_portal_url, "complete": partners.is_complete,
        "adv_roles": tiktok_api.ADVERTISER_ROLES, "tt_roles": tiktok_api.TT_ACCOUNT_ROLES,
    })


@router.get("/partners/assets")
def partner_assets(bc_id: str = "", type: str = "ADVERTISER", db: Session = Depends(get_db)):
    """JSON for the form pickers: ad accounts / TikTok accounts a BC can see."""
    token = queries.any_access_token(db)
    if not token or not bc_id.strip():
        return JSONResponse({"items": [], "error": "TikTok not connected" if not token else "no BC"})
    if type not in tiktok_api.BC_ASSET_TYPES:
        return JSONResponse({"items": [], "error": "unknown asset type"}, status_code=400)
    try:
        items = partners.assets(token, bc_id.strip(), type)
    except tiktok_api.TikTokError as e:
        return JSONResponse({"items": [], "error": f"{e.message} (code {e.code})"})
    return JSONResponse({"items": items})


@router.post("/partners/create")
async def create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    token = queries.any_access_token(db)
    if not token:
        return _back(err="TikTok isn't connected — connect first.")
    bc_id = str(form.get("bc_id") or "").strip()
    partner_id = str(form.get("partner_id") or "").strip()
    invite_email = str(form.get("invite_email") or "").strip()
    invite_bc_id = str(form.get("invite_bc_id") or "").strip() or partner_id
    if not bc_id:
        return _back(err="Pick your main Business Center.")
    if not partner_id and not invite_email:
        return _back(err="Give a partner BC ID, an email to invite, or both — there's nothing to do otherwise.")
    if partner_id and partner_id == bc_id:
        return _back(err="The partner BC must be a different Business Center than the main one.")
    if invite_email and ("@" not in invite_email or " " in invite_email):
        return _back(err="That email doesn't look right.")
    invite_role = str(form.get("invite_role") or "STANDARD").upper()
    if invite_role not in tiktok_api.BC_USER_ROLES:
        invite_role = "STANDARD"
    share_role = str(form.get("share_advertiser_role") or "OPERATOR").upper()
    inv_role = str(form.get("invite_advertiser_role") or "OPERATOR").upper()
    if share_role not in tiktok_api.ADVERTISER_ROLES:
        share_role = "OPERATOR"
    if inv_role not in tiktok_api.ADVERTISER_ROLES:
        inv_role = "OPERATOR"
    tt_roles = [r for r in form.getlist("tt_account_roles") if r in tiktok_api.TT_ACCOUNT_ROLES] or ["POST"]
    if "PUBLISH_VIDEO_ADS_ONLY" in tt_roles and "PUBLISH_VIDEO_SHOW_ON_TT_PROFILE_AND_ADS" in tt_roles:
        return _back(err="Pick either “only show as ads” or “show on profile and as ads” for the TikTok account — TikTok doesn't allow both.")
    names = {bc.bc_id: bc.name for bc in db.query(models.BusinessCenter).all()}
    tt_id = str(form.get("tt_account_id") or "").strip()
    row = models.PartnerSetup(
        bc_id=bc_id, bc_name=names.get(bc_id, ""), partner_id=partner_id,
        partner_name=str(form.get("partner_name") or names.get(partner_id, "") or "").strip()[:120],
        share_advertiser_ids=_clean_ids(form.getlist("share_advertiser_ids")),
        share_advertiser_role=share_role,
        invite_bc_id=invite_bc_id if invite_email else "", invite_bc_name=names.get(invite_bc_id, ""),
        invite_email=invite_email, invite_role=invite_role,
        invite_advertiser_ids=_clean_ids(form.getlist("invite_advertiser_ids")), invite_advertiser_role=inv_role,
        tt_account_id=tt_id, tt_account_name=str(form.get("tt_account_name") or "").strip()[:120],
        tt_account_roles=",".join(tt_roles),
        partner_status=partners.PENDING if partner_id else partners.SKIPPED,
        invite_status=partners.PENDING if invite_email else partners.SKIPPED,
        assign_status=partners.PENDING if (tt_id and invite_email) else partners.SKIPPED,
        pixel_shared=False, profile_shared=False,
    )
    db.add(row)
    db.commit()
    if invite_email:
        queries.set_setting(db, "partners_last_email", invite_email)
    from .. import jobs
    jobs.enqueue(db, "partner_setup", f"Partner setup: {row.partner_name or row.partner_id or row.invite_email}",
                 {"row_id": row.id}, href=f"/partners#setup-{row.id}")
    return _back(ok="Running in the background — you'll get a notification; the row below fills in as each step answers.", anchor=f"setup-{row.id}")


@router.post("/partners/{row_id}/retry")
def retry(row_id: int, db: Session = Depends(get_db)):
    row = db.get(models.PartnerSetup, row_id)
    token = queries.any_access_token(db)
    if not row or not token:
        return _back(err="Setup not found or TikTok not connected.")
    if row.assign_status == partners.ERROR:
        row.assign_status = partners.PENDING
        db.commit()
    from .. import jobs
    jobs.enqueue(db, "partner_setup", f"Partner setup retry: {row.partner_name or row.partner_id or row.invite_email}",
                 {"row_id": row.id}, href=f"/partners#setup-{row.id}")
    return _back(ok="Retrying in the background — you'll get a notification.", anchor=f"setup-{row.id}")


@router.post("/partners/{row_id}/tick")
async def tick(row_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(models.PartnerSetup, row_id)
    if not row:
        return _back(err="Setup not found.")
    form = await request.form()
    row.pixel_shared = form.get("pixel_shared") is not None
    row.profile_shared = form.get("profile_shared") is not None
    row.note = str(form.get("note") or "")[:500]
    row.updated_at = partners._now()
    db.commit()
    return _back(ok="Checklist saved.", anchor=f"setup-{row.id}")


@router.post("/partners/{row_id}/delete")
def delete(row_id: int, db: Session = Depends(get_db)):
    row = db.get(models.PartnerSetup, row_id)
    if row:
        db.delete(row)
        db.commit()
    return _back(ok="Removed from this list (nothing changes on TikTok).")
