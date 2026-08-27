"""Identity pool — upload avatar images + paste display names; each name+avatar
pair is consumed by exactly ONE launch (unique identity per campaign).

Pairing rule: uploaded images become rows (name defaults from the file name);
the optional pasted name list assigns names to the new rows in order.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from .. import config, models
from ..database import get_db
from ..templating import render

router = APIRouter()

AVATARS_DIR = config.DATA_DIR / "identity_avatars"
ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "avatar.jpg")[:120]


def _pretty_name(file_name: str) -> str:
    base = file_name.rsplit(".", 1)[0]
    return re.sub(r"[_-]+", " ", base).strip().title()[:100]


@router.get("/identities")
def identities_page(request: Request, db: Session = Depends(get_db)):
    rows = (db.query(models.AdIdentity)
            .order_by(models.AdIdentity.status, models.AdIdentity.id.desc()).all())
    accounts = {a.advertiser_id: (a.advertiser_name or a.advertiser_id)
                for a in db.query(models.AdAccount).all()}
    available = sum(1 for r in rows if r.status == "available")
    return render(request, "identities.html", {
        "rows": rows, "accounts": accounts, "available": available,
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
        "title": "Identities",
    })


@router.post("/identities/upload")
async def upload_identities(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    files = [v for v in form.getlist("avatars") if isinstance(v, UploadFile)]
    names = [n.strip()[:100] for n in str(form.get("names") or "").splitlines() if n.strip()]
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    saved, skipped = 0, []
    for f in files:
        fname = _safe_name(f.filename)
        ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
        if ext not in ALLOWED_IMAGE:
            skipped.append(f"{fname}: not an image")
            continue
        data = await f.read()
        if not data:
            skipped.append(f"{fname}: empty file")
            continue
        if len(data) > MAX_IMAGE_BYTES:
            skipped.append(f"{fname}: over 5MB")
            continue
        row = models.AdIdentity(
            display_name=(names[saved] if saved < len(names) else _pretty_name(fname)))
        db.add(row)
        db.flush()
        path = AVATARS_DIR / f"{row.id}_{fname}"
        path.write_bytes(data)
        row.avatar_path = str(path)
        db.commit()
        saved += 1
    # names beyond the number of images are ignored on purpose (they have no avatar)
    q = f"ok={saved}+identities+added"
    if len(names) > saved and files:
        q += f"&err={len(names) - saved}+name(s)+had+no+image+—+upload+more+avatars"
    elif names and not files:
        q = "err=upload+avatar+images+too+—+every+identity+needs+one"
    if skipped:
        q += "&err=" + "+·+".join(skipped)[:250].replace(" ", "+")
    return RedirectResponse(f"/identities?{q}", status_code=303)


@router.post("/identities/{identity_id}/update")
async def update_identity(identity_id: int, request: Request,
                          db: Session = Depends(get_db)):
    form = await request.form()
    row = db.get(models.AdIdentity, identity_id)
    if not row:
        return RedirectResponse("/identities?err=not+found", status_code=303)
    if row.status == "used":
        return RedirectResponse("/identities?err=already+used+—+locked", status_code=303)
    name = str(form.get("display_name") or "").strip()[:100]
    if name:
        row.display_name = name
        db.commit()
    return RedirectResponse("/identities?ok=saved", status_code=303)


@router.post("/identities/{identity_id}/delete")
def delete_identity(identity_id: int, db: Session = Depends(get_db)):
    row = db.get(models.AdIdentity, identity_id)
    if not row:
        return RedirectResponse("/identities?err=not+found", status_code=303)
    if row.status == "used":
        return RedirectResponse("/identities?err=already+used+—+kept+for+history",
                                status_code=303)
    try:
        if row.avatar_path:
            from pathlib import Path
            Path(row.avatar_path).unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(row)
    db.commit()
    return RedirectResponse("/identities?ok=deleted", status_code=303)
