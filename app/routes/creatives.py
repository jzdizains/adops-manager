"""Creative Library — upload video creatives once, launch them anywhere.

Each creative is consumed by exactly ONE launch (the engine reserves the next
available one, uploads it into the target account's TikTok asset library, and
records where it went). Ads publish under each account's own TikTok identity
as ad-only (dark) posts — TikTok no longer supports custom identities.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session

from .. import config, models
from ..database import get_db
from ..templating import render

router = APIRouter()

CREATIVES_DIR = config.DATA_DIR / "creatives"

ALLOWED_VIDEO = {".mp4", ".mov", ".mpeg", ".avi", ".3gp", ".webm"}
MAX_VIDEO_BYTES = 500 * 1024 * 1024


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "video.mp4")[:120]


@router.get("/creatives")
def creatives_page(request: Request, db: Session = Depends(get_db)):
    rows = (db.query(models.Creative)
            .order_by(models.Creative.status, models.Creative.id.desc()).all())
    accounts = {a.advertiser_id: (a.advertiser_name or a.advertiser_id)
                for a in db.query(models.AdAccount).all()}
    available = sum(1 for r in rows if r.status == "available")
    return render(request, "creatives.html", {
        "rows": rows, "accounts": accounts, "available": available,
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
        "title": "Creatives",
    })


@router.post("/creatives/upload")
async def upload_creatives(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    files = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    source_prefix = str(form.get("source_prefix") or "").strip()
    CREATIVES_DIR.mkdir(parents=True, exist_ok=True)
    saved, skipped = 0, []
    for f in files:
        fname = _safe_name(f.filename)
        ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
        if ext not in ALLOWED_VIDEO:
            skipped.append(f"{fname}: not a supported video type")
            continue
        # stream to a temp file in 1MB chunks — NEVER the whole video in memory
        # (a single large read once blew the server's memory limit)
        import os as _os
        tmp_path = CREATIVES_DIR / f".upload_{fname}"
        hasher = hashlib.md5()
        size = 0
        too_big = False
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    too_big = True
                    break
                hasher.update(chunk)
                out.write(chunk)
        if too_big or size == 0:
            tmp_path.unlink(missing_ok=True)
            skipped.append(f"{fname}: {'over 500MB' if too_big else 'empty file'}")
            continue
        md5 = hasher.hexdigest()
        if db.query(models.Creative).filter_by(md5=md5).first():
            tmp_path.unlink(missing_ok=True)
            skipped.append(f"{fname}: duplicate (same file already in the library)")
            continue
        row = models.Creative(name=fname, file_name=fname, md5=md5, size_bytes=size)
        db.add(row)
        db.flush()
        path = CREATIVES_DIR / f"{row.id}_{fname}"
        _os.replace(tmp_path, path)
        row.file_path = str(path)
        if source_prefix:
            row.source = f"{source_prefix}_{row.id}"
        db.commit()
        saved += 1
    q = f"ok={saved}+uploaded"
    if skipped:
        q += "&err=" + "+·+".join(skipped)[:300].replace(" ", "+")
    return RedirectResponse(f"/creatives?{q}", status_code=303)


@router.post("/creatives/{creative_id}/update")
async def update_creative(creative_id: int, request: Request,
                          db: Session = Depends(get_db)):
    form = await request.form()
    row = db.get(models.Creative, creative_id)
    if not row:
        return RedirectResponse("/creatives?err=not+found", status_code=303)
    if "source" in form:
        new_source = str(form.get("source") or "").strip()
        if row.status == "used" and row.source and new_source != row.source:
            return RedirectResponse(
                "/creatives?err=source+is+locked+once+the+creative+has+launched+(P%26L+history)",
                status_code=303)
        row.source = new_source
    if str(form.get("name") or "").strip():
        row.name = str(form.get("name")).strip()[:120]
    db.commit()
    return RedirectResponse("/creatives?ok=saved", status_code=303)


@router.post("/creatives/{creative_id}/delete")
def delete_creative(creative_id: int, db: Session = Depends(get_db)):
    row = db.get(models.Creative, creative_id)
    if not row:
        return RedirectResponse("/creatives?err=not+found", status_code=303)
    if row.status == "used":
        return RedirectResponse(
            "/creatives?err=already+launched+—+kept+for+P%26L+history", status_code=303)
    try:
        if row.file_path:
            from pathlib import Path
            Path(row.file_path).unlink(missing_ok=True)
    except OSError:
        pass
    db.query(models.CreativeUpload).filter_by(creative_id=row.id).delete()
    db.delete(row)
    db.commit()
    return RedirectResponse("/creatives?ok=deleted", status_code=303)
