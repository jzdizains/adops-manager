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

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session

from .. import config, models, queries
from ..database import get_db
from ..settings_store import get_settings
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
    view = request.query_params.get("view", "results")
    if view == "performance":
        view = "results"
    if view not in ("results", "library", "images", "carousels", "archive"):
        view = "results"
    range_key = request.query_params.get("range", "today")
    if range_key not in ("today", "yesterday", "7d", "30d", "mtd"):
        range_key = "today"
    sort = request.query_params.get("sort", "profit")
    if sort not in creative_perf.SORTS:
        sort = "profit"
    perf, fams = [], []
    if view == "results":
        s_utc, e_utc = timeutil.range_bounds(range_key)
        perf = creative_perf.rows(db, s_utc, e_utc, today=(range_key == "today"))
        perf = [x for x in perf if x["spend"] > 0 or x["revenue"] > 0]
        perf.sort(key=creative_perf.SORTS[sort], reverse=True)
        fams = creative_perf.families(perf)
        for f in fams:
            f["cls"] = "learning" if f["spend"] < 5 else ("winner" if f["roas"] >= 1.3 else ("losing" if f["roas"] < 0.9 else "learning"))
            f["accounts"] = len({x["c"].used_advertiser_id for x in f["rows"]})
            f["clicks"] = sum(x["clicks"] for x in f["rows"])
            f["epc"] = (f["revenue"] / f["clicks"]) if f["clicks"] else 0.0
            f["thumb"] = f["best"]["c"] if f.get("best") else f["rows"][0]["c"]
            f["active"] = sum(1 for x in f["rows"] if x["active"])
        gk = {"profit": lambda f: f["profit"], "roas": lambda f: f["roas"], "spend": lambda f: f["spend"], "revenue": lambda f: f["revenue"],
              "conversions": lambda f: sum(x["conversions"] for x in f["rows"]), "ctr": lambda f: f["roas"]}[sort]
        fams.sort(key=gk, reverse=True)
    # library cross-links: creative -> its campaign's cached record
    camp_by_id = {c.campaign_id: c for c in db.query(models.CampaignRecord).all()}

    # images (separate shelf; never part of the video launch pool) + AI editing
    from .. import nanobanana
    archived_rows = [r for r in rows if r.archived]
    rows = [r for r in rows if not r.archived]
    videos = [r for r in rows if (r.kind or "video") == "video"]
    images = [r for r in rows if r.kind == "image"]
    from .. import activity as _activity
    notes = _activity.notes_for(db, "creative", [str(r.id) for r in rows + archived_rows])
    lib_q = request.query_params.get("q", "").strip().lower()
    lib_state = request.query_params.get("state", "all")
    lib_label = request.query_params.get("label", "").strip().lower()
    lib_fav = request.query_params.get("fav") == "1"
    def _lib_ok(r):
        if lib_state == "fresh" and r.status != "available":
            return False
        if lib_state == "used" and r.status != "used":
            return False
        if lib_fav and not r.favorite:
            return False
        if lib_label and lib_label not in (r.labels or "").split(","):
            return False
        if lib_q and lib_q not in (r.name or "").lower() and lib_q not in (r.source or "").lower() and lib_q not in notes.get(str(r.id), "").lower():
            return False
        return True
    lib_videos = [r for r in videos if _lib_ok(r)]
    all_labels: dict[str, int] = {}
    for r in videos:
        for l in (r.labels or "").split(","):
            if l:
                all_labels[l] = all_labels.get(l, 0) + 1
    available = sum(1 for r in videos if r.status == "available")
    processing = sum(1 for r in rows if r.status == "processing")
    nb_models = [{"id": mid, "label": lbl, "prices": prices}
                 for mid, (lbl, prices) in nanobanana.MODELS.items()]

    # image shelf tags (for the filter chips): original / AI edit / text copy,
    # plus which carousel (if any) uses the image as a slide
    import json as _json
    in_carousel: dict[int, str] = {}
    for cz in rows:
        if cz.kind == "carousel":
            try:
                for sid in _json.loads(cz.carousel_images or "[]"):
                    in_carousel.setdefault(int(sid), cz.name)
            except (ValueError, TypeError):
                pass
    img_tags = {}
    for r in images:
        if r.ai_model:
            tag = "ai"
        elif r.text_spec or "_txt" in (r.name or "") or (r.ai_prompt or "").startswith("text:"):
            tag = "text"
        else:
            tag = "original"
        img_tags[r.id] = {"tag": tag, "carousel": in_carousel.get(r.id, "")}
    have_file = {r.id for r in images if r.file_path}
    editable_text = {r.id: r.text_parent_id for r in images if r.text_spec and r.text_parent_id in have_file}
    img_counts = {"all": len(images), "original": sum(1 for t in img_tags.values() if t["tag"] == "original"),
                  "ai": sum(1 for t in img_tags.values() if t["tag"] == "ai"),
                  "text": sum(1 for t in img_tags.values() if t["tag"] == "text"),
                  "carousel": sum(1 for t in img_tags.values() if t["carousel"])}

    # image dimensions + the carousel size each will be delivered in (PIL reads
    # just the header here — no pixel decode)
    dims = {}
    try:
        from PIL import Image as _Img

        from .. import image_fit
    except Exception:          # image tooling unavailable → page still renders
        _Img = None
    if _Img is not None:
        for r in images:
            if r.file_path:
                try:
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

    from .. import text_overlay
    return render(request, "creatives.html", {
        "fonts": text_overlay.available_fonts(), "default_font": text_overlay.default_font(),
        "carousels": carousels, "slide_map": slide_map, "dims": dims,
        "image_pool": [r for r in images if r.status == "available"],
        "browse_account": browse,
        "source_mode": get_settings(db).get("source_mode", "campaign"),
        "rows": videos, "images": images, "img_tags": img_tags, "img_counts": img_counts, "editable_text": editable_text,
        "accounts": accounts, "available": available,
        "nb_configured": nanobanana.configured(), "nb_models": nb_models,
        "nb_default": nanobanana.DEFAULT_MODEL, "nb_aspects": nanobanana.ASPECTS,
        "nb_models_json": __import__("json").dumps({m["id"]: m["prices"] for m in nb_models}),
        "processing": processing, "tp_configured": tp_configured,
        "tp_models": tp_models, "tp_error": tp_error,
        "uniquify_ok": video_freshen.available(),
        "view": view, "range_key": range_key, "sort": sort,
        "perf": perf, "families": fams, "camp_by_id": camp_by_id,
        "lib_videos": lib_videos, "lib_q": lib_q, "lib_state": lib_state, "lib_label": lib_label, "lib_fav": lib_fav,
        "all_labels": sorted(all_labels.items(), key=lambda kv: (-kv[1], kv[0])), "notes": notes,
        "archived_rows": archived_rows, "n_videos": len(videos), "n_fresh": sum(1 for r in videos if r.status == "available"),
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
    from fastapi.responses import Response
    row = db.get(models.Creative, creative_id)
    if not row or not row.file_path:
        return Response("not found", status_code=404)          # media URLs answer 404, never an HTML page
    p = Path(row.file_path).resolve()
    try:
        p.relative_to(CREATIVES_DIR.resolve())     # never serve outside the store
    except ValueError:
        return Response("blocked", status_code=403)
    if not p.exists():
        return Response("file missing", status_code=404)
    ext = p.suffix.lower()
    return FileResponse(str(p), media_type=_MIME.get(ext, "application/octet-stream"),
                        filename=row.file_name or p.name)


THUMB_DIR = CREATIVES_DIR / "_thumbs"
import threading as _threading
# Thumbnails and posters are generated on first request. A grid of hundreds of tiles
# arrives as hundreds of parallel requests; without a gate that is hundreds of ffmpeg /
# Pillow decodes at once — which is exactly what took the 512 MB Render instance down.
_DECODE_GATE = _threading.BoundedSemaphore(2)
THUMB_PX = 320


@router.get("/creatives/{creative_id}/thumb")
def creative_thumb(creative_id: int, db: Session = Depends(get_db)):
    """Small JPEG thumbnail for image creatives (generated once, cached on disk).
    Every grid/strip uses this instead of the original — a 2K PNG decoded at
    native size per tile is what made the browser tab balloon in memory."""
    from pathlib import Path
    row = db.get(models.Creative, creative_id)
    # an <img> must never be redirected to an HTML page: a missing file used to trigger a
    # full Creatives page render per broken tile — that is what made the page crawl
    if not row or row.kind != "image" or not row.file_path:
        return RedirectResponse("/static/no-poster.svg", status_code=303)
    src = Path(row.file_path).resolve()
    try:
        src.relative_to(CREATIVES_DIR.resolve())
    except ValueError:
        return RedirectResponse("/static/no-poster.svg", status_code=303)
    if not src.exists():
        return RedirectResponse("/static/no-poster.svg", status_code=303)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / f"{row.id}_{(row.md5 or 'x')[:12]}.jpg"
    if not out.exists():
        try:
            from PIL import Image, ImageOps
            with _DECODE_GATE:
                if not out.exists():
                    with Image.open(src) as im:
                        im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))   # JPEG: decode at reduced size
                        im = ImageOps.exif_transpose(im)                 # phone photos carry rotation in EXIF
                        im = im.convert("RGB")
                        im.thumbnail((THUMB_PX, THUMB_PX))
                        im.save(out, "JPEG", quality=82, optimize=True)
        except Exception:                      # unreadable image → a placeholder, never the multi-MB original
            return RedirectResponse("/static/no-poster.svg", status_code=303)
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
async def _save_image_upload(db: Session, f: UploadFile, source_prefix: str) -> str:
    """Store one uploaded image on the image shelf. Returns "" on success or the
    reason it was skipped (bad type / size / duplicate / disk problem)."""
    fname = _safe_name(f.filename)
    ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
    if ext not in ALLOWED_IMAGE:
        return f"{fname}: not a supported image type (png/jpg/webp)"
    data = await f.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        return f"{fname}: {'over 25MB' if data else 'empty file'}"
    md5 = hashlib.md5(data).hexdigest()
    if db.query(models.Creative).filter_by(md5=md5).first():
        return f"{fname}: duplicate"
    row = models.Creative(name=fname, file_name=fname, md5=md5, source_md5=md5,
                          size_bytes=len(data), kind="image")
    db.add(row)
    db.flush()
    path = CREATIVES_DIR / f"{row.id}_{fname}"
    try:
        with open(path, "wb") as out:
            out.write(data)
    except OSError as e:
        db.rollback()
        return f"{fname}: couldn't write to the data disk ({e.strerror or e})"
    row.file_path = str(path)
    if source_prefix:
        row.source = f"{source_prefix}_{row.id}"
    db.commit()
    return ""


@router.post("/creatives/upload-images")
async def upload_images(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    files = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    source_prefix = str(form.get("source_prefix") or "").strip()
    CREATIVES_DIR.mkdir(parents=True, exist_ok=True)
    saved, skipped = 0, []
    for f in files:
        why = await _save_image_upload(db, f, source_prefix)
        if why:
            skipped.append(why)
        else:
            saved += 1
    q = f"ok={saved}+image(s)+uploaded" if saved else "ok=nothing+uploaded"
    if skipped:
        q += "&err=" + "+·+".join(skipped)[:300].replace(" ", "+")
    return RedirectResponse(f"/creatives?view=images&{q}", status_code=303)


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
    return RedirectResponse(f"/creatives?view=images&ok={variants}+AI+edit(s)+started+(≈${est:.2f})",
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
    return RedirectResponse(f"/creatives?view=images&ok={variants}+image(s)+generating+(≈${est:.2f})",
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
    saved, queued, images, skipped = 0, 0, 0, []
    for f in files:
        fname = _safe_name(f.filename)
        ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
        if ext in ALLOWED_IMAGE:
            # images dropped into the main uploader go straight to the image shelf
            # (variations / TensorPix are video-only)
            why = await _save_image_upload(db, f, source_prefix)
            if why:
                skipped.append(why)
            else:
                images += 1
            continue
        if ext not in ALLOWED_VIDEO:
            skipped.append(f"{fname}: not a video (mp4/mov/webm) or image (png/jpg/webp)")
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
        parts.append(f"{saved} video(s) uploaded")
    if queued:
        parts.append(f"{queued} sent to TensorPix (appear as they finish)")
    if images:
        parts.append(f"{images} image(s) added to the image shelf below")
    q = "ok=" + (", ".join(parts).replace(" ", "+") or "nothing+to+do")
    if skipped:
        q += "&err=" + "+·+".join(skipped)[:300].replace(" ", "+")
    return RedirectResponse(f"/creatives?view={'images' if (images and not saved and not queued) else 'library'}&{q}", status_code=303)


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
    return RedirectResponse("/creatives?view=library&ok=saved", status_code=303)


def _image_delete_block(db: Session, row: models.Creative) -> str:
    """Why an image can't be deleted ("" = it can). Launched images may go —
    campaigns on TikTok keep their uploaded copy; only a carousel slide is
    protected (delete the carousel first, or delete it with its slides)."""
    if row.kind == "image":
        import json as _json
        for cz in db.query(models.Creative).filter_by(kind="carousel").all():
            try:
                if row.id in [int(x) for x in _json.loads(cz.carousel_images or "[]")]:
                    return f"slide in carousel “{cz.name}”"
            except (ValueError, TypeError):
                pass
    return ""


def _delete_row(db: Session, row: models.Creative) -> None:
    try:
        if row.file_path:
            from pathlib import Path
            Path(row.file_path).unlink(missing_ok=True)
    except OSError:
        pass
    db.query(models.CreativeUpload).filter_by(creative_id=row.id).delete()
    db.delete(row)


@router.post("/creatives/images/bulk-delete")
async def bulk_delete_images(request: Request, db: Session = Depends(get_db)):
    """Delete the ticked images; anything in use is skipped and named."""
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    done, skipped = 0, []
    for cid in ids:
        row = db.get(models.Creative, cid)
        if not row or row.kind != "image":
            continue
        why = _image_delete_block(db, row)
        if why:
            skipped.append(f"{row.name}: {why}")
            continue
        _delete_row(db, row)
        done += 1
    db.commit()
    q = f"ok={done}+image(s)+deleted" if done else "ok=nothing+deleted"
    if skipped:
        q += "&err=kept+" + "+·+".join(skipped)[:300].replace(" ", "+")
    return RedirectResponse(f"/creatives?view=images&{q}", status_code=303)


@router.post("/creatives/{creative_id}/delete")
async def delete_creative(request: Request, creative_id: int, db: Session = Depends(get_db)):
    """Delete a creative for good. Carousels: with_images=1 also removes its
    slides (those not used by another carousel). Launched creatives may be
    deleted — TikTok keeps its own copy; their rows vanish from Results, so
    Archive is the option that keeps history. Fetch callers get JSON."""
    from fastapi.responses import JSONResponse
    import json as _json
    form = await request.form()
    wants_json = request.headers.get("x-requested-with") == "fetch"
    row = db.get(models.Creative, creative_id)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404) if wants_json else RedirectResponse("/creatives?err=not+found", status_code=303)
    if row.kind == "image":
        why = _image_delete_block(db, row)
        if why:
            return JSONResponse({"ok": False, "error": f"“{row.name}” is a {why} — delete that carousel first (or delete it with its slides)."}, status_code=400) if wants_json else \
                RedirectResponse(f"/creatives?view=images&err=“{row.name}”+is+a+{why.replace(' ', '+')}+—+delete+that+carousel+first", status_code=303)
    removed_images = 0
    if row.kind == "carousel" and str(form.get("with_images") or "") in ("1", "true", "on"):
        mine = _slides_of(row)
        others: set[int] = set()
        for cz in db.query(models.Creative).filter(models.Creative.kind == "carousel", models.Creative.id != row.id):
            others.update(_slides_of(cz))
        for iid in mine:
            if iid in others:
                continue
            img = db.get(models.Creative, iid)
            if img is not None:
                _delete_row(db, img); removed_images += 1
    view = "carousels" if row.kind == "carousel" else ("images" if row.kind == "image" else "library")
    _delete_row(db, row)
    db.commit()
    if wants_json:
        return JSONResponse({"ok": True, "removed_images": removed_images})
    msg = "deleted" + (f"+with+{removed_images}+slide(s)" if removed_images else "")
    return RedirectResponse(f"/creatives?view={view}&ok={msg}", status_code=303)


# ============================================================================
# TEXT ON IMAGES — TikTok Sans, baked server-side at native resolution
# ============================================================================
@router.post("/creatives/{creative_id}/text/preview")
async def text_preview(creative_id: int, request: Request, db: Session = Depends(get_db)):
    """The SAME renderer as the save, downscaled for the editor stage — so
    the preview is the file, not a CSS approximation of it."""
    from .. import text_overlay
    row = db.get(models.Creative, creative_id)
    if not row or row.kind != "image" or not row.file_path:
        return Response(status_code=404)
    form = await request.form()
    try:
        max_w = max(120, min(int(form.get("max_w") or 600), 1600))
    except ValueError:
        max_w = 600
    try:
        data = text_overlay.render(row.file_path, dict(form), max_width=max_w, quality=82)
    except ValueError as e:
        return Response(str(e), status_code=422, media_type="text/plain")
    except OSError as e:
        return Response(f"image unreadable: {e}", status_code=404, media_type="text/plain")
    except Exception as e:  # noqa: BLE001 — the editor shows this instead of silently falling back
        import logging
        logging.getLogger("adops.creatives").exception("text preview failed")
        return Response(f"{type(e).__name__}: {str(e)[:200]}", status_code=500, media_type="text/plain")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/creatives/{creative_id}/text")
async def add_text(creative_id: int, request: Request, db: Session = Depends(get_db)):
    from .. import text_overlay
    row = db.get(models.Creative, creative_id)
    if not row or row.kind != "image" or not row.file_path:
        return RedirectResponse("/creatives?err=pick+an+image", status_code=303)
    import json as _json
    form = await request.form()
    back = "/creatives?view=carousels" if form.get("back") == "carousels" else "/creatives?view=images"
    sep = "&" if "?" in back else "?"
    try:
        spec = text_overlay.clean(dict(form))
        png = text_overlay.render(row.file_path, dict(form))
    except ValueError as e:
        return RedirectResponse(f"{back}{sep}err={quote(str(e))}", status_code=303)
    except OSError as e:
        msg = "couldn't read the image: " + str(e)[:80]
        return RedirectResponse(f"{back}{sep}err={quote(msg)}", status_code=303)
    recipe = _json.dumps({**spec, "text": "\n".join(spec["lines"])}, ensure_ascii=False)
    ext = ".jpg" if row.file_path.lower().endswith((".jpg", ".jpeg")) else ".png"
    CREATIVES_DIR.mkdir(parents=True, exist_ok=True)
    # editing an existing text copy: overwrite it in place (same card, same name)
    replace_id = str(request.query_params.get("replace") or "")
    if replace_id.isdigit():
        copy = db.get(models.Creative, int(replace_id))
        if not copy or copy.kind != "image" or copy.text_parent_id != row.id:
            return RedirectResponse(f"{back}{sep}err={quote('That text copy no longer exists — saved nothing.')}", status_code=303)
        if copy.status == "used":
            return RedirectResponse(f"{back}{sep}err={quote(f'“{copy.name}” has already launched — its file is kept as is. Save the edit as a new copy instead.')}", status_code=303)
        path = CREATIVES_DIR / f"{copy.id}_{copy.file_name or copy.name}"
        path.write_bytes(png)
        copy.file_path, copy.md5, copy.size_bytes = str(path), hashlib.md5(png).hexdigest(), len(png)
        copy.text_spec, copy.ai_prompt = recipe, f"text: {spec['lines'][0][:200]}"
        db.commit()      # thumbnails are keyed by md5, so the card refreshes by itself
        return RedirectResponse(f"{back}{sep}ok={quote(f'Updated the text on “{copy.name}”')}#c{copy.id}", status_code=303)
    base = (row.name or "image").rsplit(".", 1)[0]
    n = db.query(models.Creative).filter(models.Creative.name.like(f"{base}_txt%")).count() + 1
    fname = _safe_name(f"{base}_txt{n}{ext}")
    new = models.Creative(name=fname, file_name=fname, kind="image", status="available",
                          md5=hashlib.md5(png).hexdigest(), size_bytes=len(png),
                          source_md5=row.source_md5 or row.md5, source=row.source,
                          ai_prompt=f"text: {spec['lines'][0][:200]}", text_spec=recipe, text_parent_id=row.id)
    db.add(new)
    db.flush()
    path = CREATIVES_DIR / f"{new.id}_{fname}"
    path.write_bytes(png)
    new.file_path = str(path)
    db.commit()
    return RedirectResponse(f"{back}{sep}ok={quote(f'Saved “{fname}” with your text')}#c{new.id}", status_code=303)


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


@router.get("/creatives/music/library")
def music_library_browse(request: Request, db: Session = Depends(get_db)):
    """JSON: browse the cached Audio Library (carousel-usable tracks only) —
    q, style, page, size, sort=name|duration. Stale preview urls are refreshed."""
    from .. import music_library as ML
    qp = request.query_params
    try:
        page = max(1, int(qp.get("page") or 1))
    except ValueError:
        page = 1
    res = ML.browse(db, q=qp.get("q", ""), style=qp.get("style", ""), page=page,
                    size=60, sort=qp.get("sort", "name"))
    ML.fresh_urls(db, res["rows"])
    at = ML.synced_at(db)
    return {"ok": True, "musics": [ML.as_json(r) for r in res["rows"]], "total": res["total"],
            "page": res["page"], "pages": res["pages"], "styles": ML.styles(db),
            "synced_at": at.isoformat(timespec="minutes") if at else "",
            "status": queries.get_setting(db, ML.SETTING_STATUS, ""),
            "syncing": bool(__import__("app.jobs", fromlist=["pending"]).pending(db, "music_sync"))}


@router.post("/creatives/music/sync")
def music_library_sync(db: Session = Depends(get_db)):
    from .. import jobs
    if not _browse_account(db):
        return RedirectResponse("/creatives?view=carousels&err=" + quote("Connect TikTok first — the music library is read through an ad account."), status_code=303)
    job, created = jobs.enqueue_once(db, "music_sync", "Refresh TikTok music library", {}, href="/creatives?view=carousels")
    msg = ("Refreshing the music library in the background — every page of TikTok's Audio Library, then a carousel check per 100 tracks. Watch it on the Jobs page."
           if created else f"A library refresh is already {job.status}" + (f" · {job.progress}" if job.progress else "") + ".")
    return RedirectResponse("/creatives?view=carousels&ok=" + quote(msg), status_code=303)


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
            try:
                kpage = max(1, int(request.query_params.get("page") or 1))
            except ValueError:
                kpage = 1
            data = tiktok_api.get_music(acct.access_token, acct.advertiser_id, "SEARCH_BY_KEYWORD",
                                        keyword=q, page=kpage, page_size=200)
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
    info = data.get("page_info") or {}
    return {"ok": True, "musics": musics, "account": acct.advertiser_name or acct.advertiser_id,
            "total": info.get("total_number", len(musics)), "page": info.get("page", 1), "pages": info.get("total_page", 1)}


# ---- shared creative picker (Launch, Presets, Creatives) --------------------
@router.get("/creatives/{creative_id}/poster")
def creative_poster(creative_id: int, db: Session = Depends(get_db)):
    """First-frame JPEG for a video creative (ffmpeg, made once, cached on disk).
    Images redirect to /thumb; carousels use their first slide."""
    import subprocess
    from pathlib import Path
    from fastapi.responses import FileResponse
    from .. import video_freshen
    row = db.get(models.Creative, creative_id)
    if not row:
        return RedirectResponse("/static/no-poster.svg", status_code=303)
    if row.kind == "image":
        return RedirectResponse(f"/creatives/{row.id}/thumb", status_code=303)
    if row.kind == "carousel":
        import json as _json
        try:
            first = int((_json.loads(row.carousel_images or "[]") or [0])[0])
        except (ValueError, TypeError, IndexError):
            first = 0
        return RedirectResponse(f"/creatives/{first}/thumb" if first else "/static/no-poster.svg", status_code=303)
    if not row.file_path:
        return RedirectResponse("/static/no-poster.svg", status_code=303)
    src = Path(row.file_path).resolve()
    try:
        src.relative_to(CREATIVES_DIR.resolve())
    except ValueError:
        return RedirectResponse("/static/no-poster.svg", status_code=303)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / f"v{row.id}_{(row.md5 or 'x')[:12]}.jpg"
    if not out.exists() and src.exists():
        _make_poster(src, out)
    if not out.exists():
        return RedirectResponse("/static/no-poster.svg", status_code=303)
    return FileResponse(str(out), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _make_poster(src, out) -> bool:
    """First frame → small JPEG, one ffmpeg at a time (gate), single-threaded,
    no audio decode. Called on upload (video_freshen) and lazily by /poster."""
    import subprocess
    from .. import video_freshen
    try:
        with _DECODE_GATE:
            if out.exists():
                return True
            subprocess.run([video_freshen.ffmpeg_exe(), "-y", "-loglevel", "error", "-threads", "1", "-ss", "0.5", "-i", str(src),
                            "-an", "-sn", "-frames:v", "1", "-vf", "scale=270:-2", "-q:v", "5", str(out)], timeout=20, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return False
    return out.exists()


def ensure_poster(row) -> None:
    """Best-effort poster for a video creative (used right after upload so the
    library never has to generate posters under page load)."""
    from pathlib import Path
    if not row or row.kind != "video" or not row.file_path:
        return
    src = Path(row.file_path)
    if not src.exists():
        return
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    _make_poster(src, THUMB_DIR / f"v{row.id}_{(row.md5 or 'x')[:12]}.jpg")


@router.get("/creatives/pick.json")
def creatives_pick(request: Request, db: Session = Depends(get_db)):
    """The picker's data: creatives of a kind with state, upload time, note,
    labels, all-time P&L. ?kind=video|carousel|image&state=fresh|used|all&q="""
    from fastapi.responses import JSONResponse
    from .. import activity, creative_perf, timeutil
    kind = request.query_params.get("kind", "video")
    if kind not in ("video", "carousel", "image"):
        kind = "video"
    state = request.query_params.get("state", "all")
    q = request.query_params.get("q", "").strip().lower()
    rows = (db.query(models.Creative).filter(models.Creative.kind == kind, models.Creative.status != "processing",
                                             (models.Creative.error == "") | (models.Creative.error.is_(None)))
            .order_by(models.Creative.id.desc()).all())
    # all-time results per creative (DB only)
    start = timeutil.local_midnight_utc(-365); end = timeutil.local_midnight_utc(1)
    perf = {r["c"].id: r for r in creative_perf.rows(db, start, end)}
    fam_ct: dict[str, int] = {}
    for r in rows:
        fam_ct[r.source_md5 or r.md5 or ""] = fam_ct.get(r.source_md5 or r.md5 or "", 0) + 1
    notes = activity.notes_for(db, "creative", [str(r.id) for r in rows])
    out = []
    for r in rows:
        st = "used" if r.status == "used" else "fresh"
        if state == "fresh" and st != "fresh":
            continue
        if state == "used" and st != "used":
            continue
        if q and q not in (r.name or "").lower() and q not in (r.source or "").lower() and q not in (notes.get(str(r.id), "") or "").lower():
            continue
        p = perf.get(r.id)
        item = {"id": r.id, "name": r.name, "kind": r.kind, "state": st, "source": r.source or "",
                "uploaded_at": (r.uploaded_at.isoformat() + "Z") if r.uploaded_at else "",
                "uploaded_ago": _ago(r.uploaded_at), "size_mb": round((r.size_bytes or 0) / 1e6, 1),
                "poster": f"/creatives/{r.id}/poster", "file": f"/creatives/{r.id}/file" if r.kind != "carousel" else "",
                "variants": fam_ct.get(r.source_md5 or r.md5 or "", 1), "variant": bool(r.freshen or r.uniquify),
                "note": notes.get(str(r.id), ""), "music": r.music_name or "",
                "slides": len(_slides_of(r)) if r.kind == "carousel" else 0,
                "slide_ids": _slides_of(r) if r.kind == "carousel" else [],
                "spend": round(p["spend"], 2) if p else 0.0, "revenue": round(p["revenue"], 2) if p else 0.0,
                "profit": round(p["profit"], 2) if p else 0.0, "roas": round(p["roas"], 2) if p else 0.0,
                "used_in": (p["campaign_name"] if p else ""), "used_account": (p["account_name"] if p else "")}
        out.append(item)
    return JSONResponse({"items": out, "kind": kind, "state": state,
                         "counts": {"fresh": sum(1 for r in rows if r.status != "used"), "used": sum(1 for r in rows if r.status == "used")}})


def _slides_of(row) -> list:
    import json as _json
    try:
        return [int(x) for x in _json.loads(row.carousel_images or "[]")]
    except (ValueError, TypeError):
        return []


def _ago(dt):
    from ..templating import _ago as _f
    return _f(dt)


# ---- library organisation: archive / favourite / labels / bulk --------------
def _wants_json(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", "")


@router.post("/creatives/bulk")
async def creatives_bulk(request: Request, db: Session = Depends(get_db)):
    """action=archive|restore|favorite|unfavorite|label|unlabel|delete, ids=1,2,3[, label=x]."""
    from fastapi.responses import JSONResponse
    from .. import activity
    form = await request.form()
    action = form.get("action", "")
    ids = [int(x) for x in str(form.get("ids", "")).replace(" ", "").split(",") if x.isdigit()]
    label = str(form.get("label", "")).strip().lower()[:40]
    rows = db.query(models.Creative).filter(models.Creative.id.in_(ids)).all() if ids else []
    n = 0
    for r in rows:
        if action == "archive":
            r.archived = True; n += 1
        elif action == "restore":
            r.archived = False; n += 1
        elif action == "favorite":
            r.favorite = True; n += 1
        elif action == "unfavorite":
            r.favorite = False; n += 1
        elif action in ("label", "unlabel") and label:
            cur = [x for x in (r.labels or "").split(",") if x]
            if action == "label" and label not in cur:
                cur.append(label)
            if action == "unlabel" and label in cur:
                cur.remove(label)
            r.labels = ",".join(cur); n += 1
        elif action == "delete":
            block = _image_delete_block(db, r) if r.kind == "image" else ""
            if block:
                continue
            _delete_row(db, r); n += 1
    db.commit()
    if n:
        activity.record(db, "creative", ",".join(str(i) for i in ids[:20]), action, f"{n} creative(s)" + (f" · {label}" if label else ""), request=request)
    if _wants_json(request):
        return JSONResponse({"ok": True, "n": n, "action": action})
    back = str(form.get("back") or "/creatives")
    return RedirectResponse(back if back.startswith("/") else "/creatives", status_code=303)


@router.get("/creatives/labels.json")
def creatives_labels(db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    counts: dict[str, int] = {}
    for (labels,) in db.query(models.Creative.labels).filter(models.Creative.labels != "", models.Creative.labels.isnot(None)):
        for l in (labels or "").split(","):
            if l:
                counts[l] = counts.get(l, 0) + 1
    return JSONResponse({"labels": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))})


@router.get("/creatives/{creative_id}/detail")
def creative_detail(creative_id: int, db: Session = Depends(get_db)):
    """Everything the creative side panel shows: results (today / all time,
    per account), where it ran, variants, note, labels, upload time."""
    from fastapi.responses import JSONResponse
    from .. import activity, creative_perf, timeutil
    r = db.get(models.Creative, creative_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    fam = r.source_md5 or r.md5 or ""
    variants = db.query(models.Creative).filter((models.Creative.source_md5 == fam) | (models.Creative.md5 == fam)).all() if fam else [r]
    vids = {v.id for v in variants}
    t0, t1 = timeutil.range_bounds("today")
    a0, a1 = timeutil.local_midnight_utc(-365), timeutil.local_midnight_utc(1)
    today = [x for x in creative_perf.rows(db, t0, t1, today=True) if x["c"].id in vids]
    alltime = [x for x in creative_perf.rows(db, a0, a1) if x["c"].id in vids]
    def _sum(rows):
        sp = sum(x["spend"] for x in rows); rv = sum(x["revenue"] for x in rows); cl = sum(x["clicks"] for x in rows)
        return {"spend": round(sp, 2), "revenue": round(rv, 2), "profit": round(rv - sp, 2), "roas": round(rv / sp, 2) if sp else 0.0,
                "epc": round(rv / cl, 2) if cl else 0.0, "tests": len(rows), "conversions": sum(x["conversions"] for x in rows)}
    by_acct = sorted(({"account": x["account_name"], "campaign": x["campaign_name"], "advertiser_id": x["c"].used_advertiser_id, "campaign_id": x["c"].used_campaign_id,
                       "spend": round(x["spend"], 2), "profit": round(x["profit"], 2), "roas": round(x["roas"], 2), "active": x["active"]} for x in alltime),
                     key=lambda x: -x["profit"])
    note = activity.get_note(db, "creative", str(r.id))
    return JSONResponse({
        "id": r.id, "name": r.name, "kind": r.kind or "video", "status": r.status, "archived": bool(r.archived), "favorite": bool(r.favorite),
        "labels": [x for x in (r.labels or "").split(",") if x], "source": r.source or "", "size_mb": round((r.size_bytes or 0) / 1e6, 1),
        "uploaded_at": (r.uploaded_at.isoformat() + "Z") if r.uploaded_at else "", "uploaded_ago": _ago(r.uploaded_at),
        "uploaded_str": r.uploaded_at.strftime("%d %b %Y · %H:%M") if r.uploaded_at else "",
        "poster": f"/creatives/{r.id}/poster", "file": f"/creatives/{r.id}/file" if r.kind != "carousel" else "",
        "music": r.music_name or "", "slides": len(_slides_of(r)) if r.kind == "carousel" else 0, "slide_ids": _slides_of(r) if r.kind == "carousel" else [],
        "variants": [{"id": v.id, "name": v.name, "status": v.status, "archived": bool(v.archived)} for v in variants if v.id != r.id][:30],
        "today": _sum(today), "alltime": _sum(alltime), "by_account": by_acct[:40], "note": note.text if note else "",
        "used_in": r.used_campaign_id or "", "used_account": r.used_advertiser_id or "",
    })


@router.post("/creatives/{creative_id}/rename")
async def creative_rename(request: Request, creative_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    form = await request.form()
    r = db.get(models.Creative, creative_id)
    if not r:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    name = str(form.get("name", "")).strip()[:120]
    if name:
        r.name = name
        db.commit()
    return JSONResponse({"ok": True, "name": r.name})
