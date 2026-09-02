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
    # ---- Performance view: which creative is making money -------------------
    from .. import creative_perf, timeutil
    view = request.query_params.get("view", "library")
    if view not in ("library", "performance", "carousels"):
        view = "library"
    range_key = request.query_params.get("range", "7d")
    if range_key not in ("today", "yesterday", "7d", "30d", "mtd"):
        range_key = "7d"
    sort = request.query_params.get("sort", "roas")
    if sort not in creative_perf.SORTS:
        sort = "roas"
    perf, fams = [], []
    if view == "performance":
        s_utc, e_utc = timeutil.range_bounds(range_key)
        perf = creative_perf.rows(db, s_utc, e_utc, today=(range_key == "today"))
        perf.sort(key=creative_perf.SORTS[sort], reverse=True)
        fams = creative_perf.families(perf)
    # library cross-links: creative -> its campaign's cached record
    camp_by_id = {c.campaign_id: c for c in db.query(models.CampaignRecord).all()}

    # images (separate shelf; never part of the video launch pool) + AI editing
    from .. import nanobanana
    videos = [r for r in rows if (r.kind or "video") == "video"]
    images = [r for r in rows if r.kind == "image"]
    available = sum(1 for r in videos if r.status == "available")
    processing = sum(1 for r in rows if r.status == "processing")
    nb_models = [{"id": mid, "label": lbl, "prices": prices}
                 for mid, (lbl, prices) in nanobanana.MODELS.items()]

    # image dimensions + the carousel size each will be delivered in (PIL reads
    # just the header here — no pixel decode)
    from .. import image_fit
    dims = {}
    for r in images:
        if r.file_path:
            try:
                from PIL import Image as _Img
                with _Img.open(r.file_path) as im:
                    w, h = im.size
                fmt, (tw, th) = image_fit.pick_format(w, h)
                dims[r.id] = {"w": w, "h": h, "tw": tw, "th": th, "fmt": fmt, "exact": (w, h) == (tw, th)}
            except Exception:
                pass

    carousels = [r for r in rows if r.kind == "carousel"]
    import json as _json
    slide_map = {}
    for cz in carousels:
        try:
            slide_map[cz.id] = [int(x) for x in _json.loads(cz.carousel_images or "[]")]
        except (ValueError, TypeError):
            slide_map[cz.id] = []
    browse = _browse_account(db)

    return render(request, "creatives.html", {
        "carousels": carousels, "slide_map": slide_map, "dims": dims,
        "image_pool": [r for r in images if r.status == "available"],
        "browse_account": browse,
        "rows": videos, "images": images, "accounts": accounts, "available": available,
        "nb_configured": nanobanana.configured(), "nb_models": nb_models,
        "nb_default": nanobanana.DEFAULT_MODEL, "nb_aspects": nanobanana.ASPECTS,
        "nb_models_json": __import__("json").dumps({m["id"]: m["prices"] for m in nb_models}),
        "processing": processing, "tp_configured": tp_configured,
        "tp_models": tp_models, "tp_error": tp_error,
        "uniquify_ok": video_freshen.available(),
        "view": view, "range_key": range_key, "sort": sort,
        "perf": perf, "families": fams, "camp_by_id": camp_by_id,
        "title": "Creatives",
    })


SRC_DIR = CREATIVES_DIR / "_src"

_MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
         ".mpeg": "video/mpeg", ".avi": "video/x-msvideo", ".3gp": "video/3gpp",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
ALLOWED_IMAGE = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 25 * 1024 * 1024


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
    return FileResponse(str(p), media_type=_MIME.get(ext, "application/octet-stream"),
                        filename=row.file_name or p.name)


THUMB_DIR = CREATIVES_DIR / "_thumbs"
THUMB_PX = 320


@router.get("/creatives/{creative_id}/thumb")
def creative_thumb(creative_id: int, db: Session = Depends(get_db)):
    """Small JPEG thumbnail for image creatives (generated once, cached on disk).
    Every grid/strip uses this instead of the original — a 2K PNG decoded at
    native size per tile is what made the browser tab balloon in memory."""
    from pathlib import Path
    row = db.get(models.Creative, creative_id)
    if not row or row.kind != "image" or not row.file_path:
        return RedirectResponse("/creatives?err=not+found", status_code=303)
    src = Path(row.file_path).resolve()
    try:
        src.relative_to(CREATIVES_DIR.resolve())
    except ValueError:
        return RedirectResponse("/creatives?err=blocked", status_code=303)
    if not src.exists():
        return RedirectResponse("/creatives?err=file+missing", status_code=303)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / f"{row.id}_{(row.md5 or 'x')[:12]}.jpg"
    if not out.exists():
        try:
            from PIL import Image
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_PX, THUMB_PX))
                im.save(out, "JPEG", quality=82, optimize=True)
        except Exception:                      # unreadable image → serve the original
            return FileResponse(str(src), media_type=_MIME.get(src.suffix.lower(), "image/png"))
    return FileResponse(str(out), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/creatives/processing")
def processing_status(db: Session = Depends(get_db)):
    """Tiny JSON the Creatives page polls instead of reloading itself every 5s."""
    n = db.query(models.Creative).filter_by(status="processing").count()
    return {"processing": n}


# ============================================================================
# IMAGES: upload + AI editing / generation (Gemini "Nano Banana")
# ============================================================================
@router.post("/creatives/upload-images")
async def upload_images(request: Request, db: Session = Depends(get_db)):
    import os as _os
    form = await request.form()
    files = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    source_prefix = str(form.get("source_prefix") or "").strip()
    CREATIVES_DIR.mkdir(parents=True, exist_ok=True)
    saved, skipped = 0, []
    for f in files:
        fname = _safe_name(f.filename)
        ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
        if ext not in ALLOWED_IMAGE:
            skipped.append(f"{fname}: not a supported image type (png/jpg/webp)")
            continue
        data = await f.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            skipped.append(f"{fname}: {'over 25MB' if data else 'empty file'}")
            continue
        md5 = hashlib.md5(data).hexdigest()
        if db.query(models.Creative).filter_by(md5=md5).first():
            skipped.append(f"{fname}: duplicate")
            continue
        row = models.Creative(name=fname, file_name=fname, md5=md5, source_md5=md5,
                              size_bytes=len(data), kind="image")
        db.add(row)
        db.flush()
        path = CREATIVES_DIR / f"{row.id}_{fname}"
        with open(path, "wb") as out:
            out.write(data)
        row.file_path = str(path)
        if source_prefix:
            row.source = f"{source_prefix}_{row.id}"
        db.commit()
        saved += 1
    q = f"ok={saved}+image(s)+uploaded" if saved else "ok=nothing+uploaded"
    if skipped:
        q += "&err=" + "+·+".join(skipped)[:300].replace(" ", "+")
    return RedirectResponse(f"/creatives?{q}#images", status_code=303)


def _ai_job_rows(db: Session, *, prompt: str, model: str, size: str, aspect: str,
                 variants: int, parent: models.Creative | None, base_name: str) -> list[int]:
    """Create N placeholder image rows (status=processing) and return their ids."""
    from .. import nanobanana
    cost = nanobanana.price(model, size)
    ids = []
    family = (parent.source_md5 or parent.md5) if parent else ""
    for n in range(variants):
        vname = f"{base_name}_ai{n + 1}.png"
        row = models.Creative(name=vname, file_name=vname, kind="image", status="processing",
                              ai_prompt=prompt, ai_model=model, ai_cost=cost,
                              source_md5=family, source=(parent.source if parent else ""))
        db.add(row)
        db.flush()
        if not family:
            row.source_md5 = f"ai{row.id}"      # its own family when generated from scratch
        ids.append(row.id)
    db.commit()
    return ids


def _run_ai_jobs(row_ids: list[int], parent_id: int | None, model: str, size: str, aspect: str):
    """Background: call Gemini once per placeholder row and store the result."""
    import os as _os
    from pathlib import Path

    from .. import nanobanana
    from ..database import SessionLocal
    d = SessionLocal()
    try:
        src_bytes, src_mime = None, "image/png"
        if parent_id:
            parent = d.get(models.Creative, parent_id)
            if parent and parent.file_path and Path(parent.file_path).exists():
                src_bytes = Path(parent.file_path).read_bytes()
                src_mime = _MIME.get(Path(parent.file_path).suffix.lower(), "image/png")
        for rid in row_ids:
            row = d.get(models.Creative, rid)
            if not row:
                continue
            try:
                out, mime = nanobanana.generate(row.ai_prompt, image=src_bytes, image_mime=src_mime,
                                                model=model, size=size, aspect=aspect)
                ext = ".jpg" if mime == "image/jpeg" else ".png"
                CREATIVES_DIR.mkdir(parents=True, exist_ok=True)
                fname = _safe_name(row.name.rsplit(".", 1)[0] + ext)
                path = CREATIVES_DIR / f"{row.id}_{fname}"
                path.write_bytes(out)
                row.file_path, row.file_name, row.name = str(path), fname, fname
                row.md5 = hashlib.md5(out).hexdigest()
                row.size_bytes = len(out)
                row.status, row.error = "available", ""
            except nanobanana.NanoBananaError as e:
                row.status, row.error = "error", e.message[:500]
            except Exception as e:      # never leave a row stuck in 'processing'
                row.status, row.error = "error", f"{type(e).__name__}: {e}"[:500]
            d.commit()
    finally:
        d.close()


def _parse_ai_form(form) -> tuple[str, str, str, str, int]:
    from .. import nanobanana
    prompt = str(form.get("prompt") or "").strip()[:2000]
    model = str(form.get("model") or nanobanana.DEFAULT_MODEL)
    if model not in nanobanana.MODELS:
        model = nanobanana.DEFAULT_MODEL
    size = str(form.get("size") or nanobanana.DEFAULT_SIZE)
    if size not in nanobanana.sizes_for(model):
        size = nanobanana.sizes_for(model)[0]
    aspect = str(form.get("aspect") or "")
    if aspect not in nanobanana.ASPECTS:
        aspect = ""
    try:
        variants = min(max(int(form.get("variants") or 1), 1), 8)
    except ValueError:
        variants = 1
    return prompt, model, size, aspect, variants


@router.post("/creatives/{creative_id}/ai-edit")
async def ai_edit(creative_id: int, request: Request, db: Session = Depends(get_db)):
    import threading

    from .. import nanobanana
    if not nanobanana.configured():
        return RedirectResponse("/creatives?err=GEMINI_API_KEY+is+not+set+—+add+it+as+an+env+var+to+enable+AI+editing",
                                status_code=303)
    row = db.get(models.Creative, creative_id)
    if not row or row.kind != "image" or not row.file_path:
        return RedirectResponse("/creatives?err=pick+an+image+creative", status_code=303)
    prompt, model, size, aspect, variants = _parse_ai_form(await request.form())
    if not prompt:
        return RedirectResponse("/creatives?err=describe+the+edit+you+want", status_code=303)
    base = (row.name or "image").rsplit(".", 1)[0]
    ids = _ai_job_rows(db, prompt=prompt, model=model, size=size, aspect=aspect,
                       variants=variants, parent=row, base_name=base)
    threading.Thread(target=_run_ai_jobs, args=(ids, row.id, model, size, aspect),
                     name="nanobanana-edit", daemon=True).start()
    est = nanobanana.price(model, size) * variants
    return RedirectResponse(f"/creatives?ok={variants}+AI+edit(s)+started+(≈${est:.2f})#images",
                            status_code=303)


@router.post("/creatives/ai-generate")
async def ai_generate(request: Request, db: Session = Depends(get_db)):
    import threading

    from .. import nanobanana
    if not nanobanana.configured():
        return RedirectResponse("/creatives?err=GEMINI_API_KEY+is+not+set+—+add+it+as+an+env+var+to+enable+AI+images",
                                status_code=303)
    form = await request.form()
    prompt, model, size, aspect, variants = _parse_ai_form(form)
    if not prompt:
        return RedirectResponse("/creatives?err=describe+the+image+you+want", status_code=303)
    base = _safe_name(str(form.get("name") or "generated").strip() or "generated").rsplit(".", 1)[0]
    ids = _ai_job_rows(db, prompt=prompt, model=model, size=size, aspect=aspect,
                       variants=variants, parent=None, base_name=base)
    threading.Thread(target=_run_ai_jobs, args=(ids, None, model, size, aspect),
                     name="nanobanana-gen", daemon=True).start()
    est = nanobanana.price(model, size) * variants
    return RedirectResponse(f"/creatives?ok={variants}+image(s)+generating+(≈${est:.2f})#images",
                            status_code=303)


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
    if row.kind == "image":
        import json as _json
        for cz in db.query(models.Creative).filter_by(kind="carousel").all():
            try:
                if row.id in [int(x) for x in _json.loads(cz.carousel_images or "[]")]:
                    return RedirectResponse(
                        f"/creatives?err=“{row.name}”+is+a+slide+in+carousel+“{cz.name}”+—+delete+that+carousel+first",
                        status_code=303)
            except (ValueError, TypeError):
                pass
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


# ============================================================================
# TEXT ON IMAGES — TikTok Sans, baked server-side at native resolution
# ============================================================================
@router.post("/creatives/{creative_id}/text")
async def add_text(creative_id: int, request: Request, db: Session = Depends(get_db)):
    from .. import text_overlay
    row = db.get(models.Creative, creative_id)
    if not row or row.kind != "image" or not row.file_path:
        return RedirectResponse("/creatives?err=pick+an+image", status_code=303)
    form = await request.form()
    back = "/creatives?view=carousels" if form.get("back") == "carousels" else "/creatives"
    try:
        png = text_overlay.render(row.file_path, dict(form))
    except ValueError as e:
        return RedirectResponse(f"{back}&err={str(e).replace(' ', '+')}" if "?" in back else f"{back}?err={str(e).replace(' ', '+')}", status_code=303)
    except OSError as e:
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}err=couldn't+read+the+image:+{str(e)[:80].replace(' ', '+')}", status_code=303)
    base = (row.name or "image").rsplit(".", 1)[0]
    ext = ".jpg" if row.file_path.lower().endswith((".jpg", ".jpeg")) else ".png"
    n = db.query(models.Creative).filter(models.Creative.name.like(f"{base}_txt%")).count() + 1
    fname = _safe_name(f"{base}_txt{n}{ext}")
    new = models.Creative(name=fname, file_name=fname, kind="image", status="available",
                          md5=hashlib.md5(png).hexdigest(), size_bytes=len(png),
                          source_md5=row.source_md5 or row.md5, source=row.source,
                          ai_prompt=f"text: {str(form.get('text') or '')[:200]}")
    db.add(new)
    db.flush()
    CREATIVES_DIR.mkdir(parents=True, exist_ok=True)
    path = CREATIVES_DIR / f"{new.id}_{fname}"
    path.write_bytes(png)
    new.file_path = str(path)
    db.commit()
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}ok=Saved+“{fname}”+with+your+text#c{new.id}", status_code=303)


# ============================================================================
# CAROUSELS: ordered image slides + a TikTok soundtrack (doc: Create Carousel Ads)
# ============================================================================
def _browse_account(db: Session):
    """The ad account used to browse TikTok's music library (any connected one)."""
    return (db.query(models.AdAccount)
            .filter(models.AdAccount.enabled == True,                 # noqa: E712
                    models.AdAccount.access_token != "",
                    models.AdAccount.status != "ACCESS_LOST")
            .order_by(models.AdAccount.advertiser_name).first())


@router.post("/creatives/carousel/save")
async def carousel_save(request: Request, db: Session = Depends(get_db)):
    import json as _json
    form = await request.form()
    name = str(form.get("name") or "").strip()[:120] or "Carousel"
    raw_ids = [x for x in str(form.get("image_ids") or "").replace(",", " ").split() if x.isdigit()]
    ids = [int(x) for x in raw_ids]
    if len(ids) < 2 or len(ids) > 35:
        return RedirectResponse("/creatives?view=carousels&err=A+carousel+needs+2+to+35+slides", status_code=303)
    imgs = {c.id: c for c in db.query(models.Creative).filter(models.Creative.id.in_(ids)).all()}
    bad = [i for i in ids if i not in imgs or imgs[i].kind != "image" or not imgs[i].file_path]
    if bad:
        return RedirectResponse("/creatives?view=carousels&err=Pick+slides+from+the+image+shelf+only", status_code=303)
    music_id = str(form.get("music_id") or "").strip()
    if not music_id:
        return RedirectResponse("/creatives?view=carousels&err=Pick+a+soundtrack+—+TikTok+requires+music+on+every+carousel", status_code=303)
    row = models.Creative(
        name=name, file_name=name, kind="carousel", status="available",
        carousel_images=_json.dumps(ids), music_id=music_id,
        music_name=str(form.get("music_name") or "").strip()[:200],
        music_author=str(form.get("music_author") or "").strip()[:200],
        source=str(form.get("source") or "").strip()[:120],
        md5=f"carousel:{','.join(map(str, ids))}:{music_id}",
        source_md5=imgs[ids[0]].source_md5 or imgs[ids[0]].md5,
        size_bytes=sum(int(imgs[i].size_bytes or 0) for i in ids))
    db.add(row)
    db.commit()
    return RedirectResponse(f"/creatives?view=carousels&ok=Carousel+“{name}”+saved+({len(ids)}+slides)#cz{row.id}",
                            status_code=303)


@router.get("/creatives/music/search")
def music_search(request: Request, db: Session = Depends(get_db)):
    """JSON for the soundtrack picker. mode = keyword | recommend | uploads | liked | history.
    'recommend' uploads the chosen slides into the browsing account first (TikTok
    recommends music for the actual images)."""
    from fastapi.responses import JSONResponse

    from .. import tiktok_api
    from .campaigns import _upload_image_to_account
    mode = request.query_params.get("mode", "keyword")
    q = request.query_params.get("q", "").strip()
    acct = _browse_account(db)
    if not acct:
        return JSONResponse({"ok": False, "error": "No connected ad account to browse TikTok's music library with."})
    try:
        if mode == "keyword":
            if not q:
                return {"ok": True, "musics": []}
            data = tiktok_api.get_music(acct.access_token, acct.advertiser_id, "SEARCH_BY_KEYWORD",
                                        keyword=q, page_size=60)
        elif mode == "recommend":
            ids = [int(x) for x in request.query_params.get("images", "").replace(",", " ").split() if x.isdigit()]
            imgs = {c.id: c for c in db.query(models.Creative).filter(models.Creative.id.in_(ids)).all()} if ids else {}
            slides = [imgs[i] for i in ids if i in imgs and imgs[i].kind == "image" and imgs[i].file_path]
            if len(slides) < 2:
                return JSONResponse({"ok": False, "error": "Pick at least 2 slides first — TikTok recommends music for the actual images."})
            urls = [_upload_image_to_account(db, acct, img)[1] for img in slides]
            if not all(urls):
                return JSONResponse({"ok": False, "error": "TikTok didn't return image URLs for the slides — try keyword search instead."})
            data = tiktok_api.get_music(acct.access_token, acct.advertiser_id, "SEARCH_BY_RECOMMEND",
                                        image_urls=urls[:35])
        elif mode == "uploads":
            data = tiktok_api.get_music(acct.access_token, acct.advertiser_id, "SEARCH_BY_SOURCE",
                                        sources=["USER"], page_size=200)
        elif mode == "liked":
            data = tiktok_api.get_music(acct.access_token, acct.advertiser_id, "SEARCH_BY_LIKED")
        elif mode == "history":
            data = tiktok_api.get_music(acct.access_token, acct.advertiser_id, "SEARCH_BY_HISTORY", page_size=100)
        else:
            return JSONResponse({"ok": False, "error": "unknown mode"})
    except tiktok_api.TikTokError as e:
        return JSONResponse({"ok": False, "error": f"TikTok: code {e.code} {e.message[:200]}"})
    musics = []
    for m in (data.get("musics") or []):
        if not m.get("music_id"):
            continue
        musics.append({
            "music_id": str(m["music_id"]), "name": m.get("name") or m.get("file_name") or "",
            "author": m.get("author") or "", "duration": m.get("duration"),
            "url": m.get("url") or "", "cover_url": m.get("cover_url") or "",
            "style": m.get("style") or "", "sources": m.get("sources") or [],
            "liked": bool(m.get("liked")), "copyright": m.get("copyright") or "",
        })
    return {"ok": True, "musics": musics, "account": acct.advertiser_name or acct.advertiser_id,
            "total": (data.get("page_info") or {}).get("total_number", len(musics))}
