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
from fastapi.responses import FileResponse, RedirectResponse
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
    processing = sum(1 for r in rows if r.status == "processing")
    from .. import tensorpix, video_freshen
    tp_configured = tensorpix.configured()
    tp_models, tp_error = [], ""
    if tp_configured:
        try:
            for m in tensorpix.list_models():
                tp_models.append({
                    "id": m.get("id"), "name": m.get("name", ""),
                    "task": tensorpix.TASKS.get(m.get("task"), ""),
                })
        except tensorpix.TensorPixError as e:
            tp_error = e.message
    return render(request, "creatives.html", {
        "rows": rows, "accounts": accounts, "available": available,
        "processing": processing, "tp_configured": tp_configured,
        "tp_models": tp_models, "tp_error": tp_error,
        "uniquify_ok": video_freshen.available(),
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
        "title": "Creatives",
    })


SRC_DIR = CREATIVES_DIR / "_src"

_MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
         ".mpeg": "video/mpeg", ".avi": "video/x-msvideo", ".3gp": "video/3gpp"}


@router.get("/creatives/{creative_id}/file")
def creative_file(creative_id: int, db: Session = Depends(get_db)):
    """Stream a creative's video for in-page preview. FileResponse handles HTTP
    Range requests, so the player can seek/scrub. Only files inside the creatives
    directory are ever served."""
    from pathlib import Path
    row = db.get(models.Creative, creative_id)
    if not row or not row.file_path:
        return RedirectResponse("/creatives?err=not+found", status_code=303)
    p = Path(row.file_path).resolve()
    try:
        p.relative_to(CREATIVES_DIR.resolve())     # never serve outside the store
    except ValueError:
        return RedirectResponse("/creatives?err=blocked", status_code=303)
    if not p.exists():
        return RedirectResponse("/creatives?err=file+missing", status_code=303)
    ext = p.suffix.lower()
    return FileResponse(str(p), media_type=_MIME.get(ext, "video/mp4"),
                        filename=row.file_name or p.name)


@router.post("/creatives/upload")
async def upload_creatives(request: Request, db: Session = Depends(get_db)):
    import os as _os

    from .. import tensorpix

    form = await request.form()
    files = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    source_prefix = str(form.get("source_prefix") or "").strip()
    do_freshen = form.get("freshen") is not None      # "create variations" toggle
    model_ids = [str(m) for m in form.getlist("model_ids") if str(m).strip().isdigit()]
    do_uniquify = form.get("uniquify") is not None
    intensity = str(form.get("intensity") or "medium")
    if intensity not in ("light", "medium", "strong"):
        intensity = "medium"
    try:
        variants = min(max(int(form.get("variants") or 1), 1), 30)
    except ValueError:
        variants = 1
    if not do_freshen:
        variants = 1     # variations only make sense when processing

    if do_freshen and not model_ids and not do_uniquify:
        return RedirectResponse(
            "/creatives?err=Pick+an+enhancement+model,+turn+on+Uniquify,+or+both.",
            status_code=303)
    if do_freshen and model_ids and not tensorpix.configured():
        return RedirectResponse(
            "/creatives?err=TensorPix+API+key+not+set+—+add+TENSORPIX_API_KEY,+or+"
            "use+Uniquify+alone.", status_code=303)

    CREATIVES_DIR.mkdir(parents=True, exist_ok=True)
    saved, queued, skipped = 0, 0, []
    for f in files:
        fname = _safe_name(f.filename)
        ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
        if ext not in ALLOWED_VIDEO:
            skipped.append(f"{fname}: not a supported video type")
            continue
        # stream to a temp file in 1MB chunks — NEVER the whole video in memory
        # (a single large read once blew the server's memory limit)
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

        if not do_freshen:
            # plain store — dedupe by exact bytes (the original behaviour)
            if db.query(models.Creative).filter_by(md5=md5).first():
                tmp_path.unlink(missing_ok=True)
                skipped.append(f"{fname}: duplicate (same file already in the library)")
                continue
            row = models.Creative(name=fname, file_name=fname, md5=md5,
                                  source_md5=md5, size_bytes=size)
            db.add(row)
            db.flush()
            path = CREATIVES_DIR / f"{row.id}_{fname}"
            _os.replace(tmp_path, path)
            row.file_path = str(path)
            if source_prefix:
                row.source = f"{source_prefix}_{row.id}"
            db.commit()
            saved += 1
            continue

        # VARIATIONS: keep ONE source copy, queue N variants for TensorPix
        SRC_DIR.mkdir(parents=True, exist_ok=True)
        src_path = SRC_DIR / f"{md5}{ext}"
        if not src_path.exists():
            _os.replace(tmp_path, src_path)
        else:
            tmp_path.unlink(missing_ok=True)   # same source already staged
        base = fname.rsplit(".", 1)[0]
        models_csv = ",".join(model_ids)
        for n in range(variants):
            vname = (f"{base}_v{n+1}{ext}" if variants > 1 else fname)
            row = models.Creative(
                name=vname, file_name=vname, size_bytes=size,
                status="processing", freshen=True, tp_model_ids=models_csv,
                uniquify=do_uniquify, freshen_intensity=intensity,
                src_path=str(src_path), source_md5=md5)
            db.add(row)
            db.flush()
            if source_prefix:
                row.source = f"{source_prefix}_{row.id}"
            queued += 1
        db.commit()

    # kick a background driver that walks the TensorPix state machine to done
    if queued:
        import threading
        import time as _time

        from .. import tensorpix_worker
        from ..database import SessionLocal

        def _run():
            d = SessionLocal()
            try:
                deadline = _time.time() + 1800   # 30-min safety cap
                while _time.time() < deadline:
                    if tensorpix_worker.pending_count(d) == 0:
                        break
                    tensorpix_worker.process_pending(d, limit=8)
                    _time.sleep(5)
            finally:
                d.close()
        threading.Thread(target=_run, name="tensorpix-kick", daemon=True).start()

    parts = []
    if saved:
        parts.append(f"{saved} uploaded")
    if queued:
        parts.append(f"{queued} sent to TensorPix (appear as they finish)")
    q = "ok=" + ("+".join(parts).replace(" ", "+") or "nothing+to+do")
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
