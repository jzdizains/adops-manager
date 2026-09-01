"""Background state-machine that turns queued creative variants (Creative rows
in status='processing') into finished, downloaded TensorPix renders.

Each row walks: upload source → wait for source ready → create a per-variant
job → poll → download → available. Steps are incremental (one hop per call) and
committed as they complete, so a restart resumes cleanly. TensorPix work is I/O
bound, so — unlike the old CPU-bound ffmpeg path — passes can overlap; a short
per-row poll throttle (tp_checked_at) keeps us from hammering the API.
"""
from __future__ import annotations

import hashlib
import os
import random
from datetime import datetime, timezone

import threading

POLL_THROTTLE_SEC = 6      # don't re-poll the same row faster than this
_pass_lock = threading.Lock()   # only one processing pass at a time (kick + sweep)


def _md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _variant_params(row) -> dict:
    """Per-variant nudges (seeded by row id) so N jobs off one source differ
    from each other AND the source, without hurting quality: crf jitter, a few
    trimmed lead frames, and a light sharpen only if a Sharpen model is chosen."""
    rng = random.Random(row.id)
    p = {"crf": 17 + (row.id % 3),                # 17..19 — visually transparent
         "start_frame": row.id % 6}               # drop 0..5 lead frames
    model_ids = _model_ids(row)
    # Sharpen task id = 4; only send sharpen_strength if such a model is present
    # (we can't know task per id here, so keep it conservative/optional)
    if rng.random() < 0.5:
        p["sharpen_strength"] = round(rng.uniform(0.5, 1.5), 2)
    return p


def _model_ids(row) -> list[int]:
    out = []
    for tok in (row.tp_model_ids or "").split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return out


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _due(row) -> bool:
    if not row.tp_checked_at:
        return True
    return (_now() - row.tp_checked_at).total_seconds() >= POLL_THROTTLE_SEC


def process_pending(db, limit: int = 6) -> int:
    """Advance up to `limit` processing rows by one hop each. Returns how many
    were touched. Never raises — failures land on row.error. Serialized so the
    post-upload kick thread and the background sweep never process the same row
    (which would collide two ffmpeg runs on one file)."""
    if not _pass_lock.acquire(blocking=False):
        return 0                      # another pass is running — let it finish
    try:
        return _process_pending(db, limit)
    finally:
        _pass_lock.release()


def _process_pending(db, limit: int) -> int:
    from . import config, models, tensorpix

    rows = (db.query(models.Creative)
            .filter(models.Creative.status == "processing")
            .order_by(models.Creative.id).all())
    rows = [r for r in rows if _due(r)][:limit]
    if not rows:
        return 0
    if not tensorpix.configured():
        # only rows that actually need TensorPix fail; uniquify-only rows proceed
        needs_tp = [r for r in rows if _model_ids(r)]
        for r in needs_tp:
            r.status = "error"
            r.error = ("TensorPix API key not set — add TENSORPIX_API_KEY in the "
                       "server environment, then re-upload.")
        if needs_tp:
            db.commit()
        rows = [r for r in rows if not _model_ids(r)]
        if not rows:
            return len(needs_tp)

    out_dir = config.DATA_DIR / "creatives"
    out_dir.mkdir(parents=True, exist_ok=True)
    touched = 0
    for row in rows:
        row.tp_checked_at = _now()
        try:
            _advance(db, row, out_dir, tensorpix, models)
        except tensorpix.TensorPixError as e:
            row.status = "error"
            row.error = e.message[:400]
        except Exception as e:                     # noqa: BLE001
            row.status = "error"
            row.error = f"{type(e).__name__}: {e}"[:400]
        db.commit()
        touched += 1
    return touched


def _store(db, models, row, out_path) -> None:
    """Record a finished variant file and mark it available."""
    row.file_path = str(out_path)
    row.md5 = _md5_file(str(out_path))
    row.size_bytes = os.path.getsize(str(out_path))
    row.status = "available"
    row.error = ""
    _gc_source(db, models, row.source_md5)


def _advance(db, row, out_dir, tensorpix, models) -> None:
    from . import video_freshen
    out = out_dir / f"{row.id}_{row.file_name}"

    # ---- uniquify-only path (no TensorPix model chosen) --------------------
    if not _model_ids(row):
        if not row.uniquify:
            row.status = "error"
            row.error = "Nothing to do: pick a TensorPix model, uniquify, or both."
            return
        if not video_freshen.available():
            row.status = "error"
            row.error = "Video processing unavailable (imageio-ffmpeg missing)."
            return
        if not row.src_path or not os.path.exists(row.src_path):
            raise RuntimeError("source file went missing before processing")
        video_freshen.uniquify(row.src_path, str(out),
                               intensity=row.freshen_intensity or "medium", seed=row.id)
        _store(db, models, row, out)
        return

    # STEP 1 — ensure the source is uploaded (share one upload per source_md5)
    if not row.tp_video_id:
        sib = (db.query(models.Creative)
               .filter(models.Creative.source_md5 == row.source_md5,
                       models.Creative.tp_video_id != "")
               .first())
        if sib:
            row.tp_video_id = sib.tp_video_id          # reuse the shared upload
        else:
            if not row.src_path or not os.path.exists(row.src_path):
                raise RuntimeError("source file went missing before upload")
            row.tp_video_id = tensorpix.upload_video(row.src_path, row.file_name)
        return   # next hop checks readiness

    # STEP 2 — wait for the source to be stored, then create this variant's job
    if not row.tp_job_id:
        if not tensorpix.video_ready(row.tp_video_id):
            return   # still uploading server-side — try again next hop
        job = tensorpix.create_job(row.tp_video_id, _model_ids(row),
                                   **_variant_params(row))
        row.tp_job_id = str(job.get("id") or "")
        try:
            row.tp_cost = float(job.get("cost_usd") or 0)
        except (TypeError, ValueError):
            row.tp_cost = 0.0
        if not row.tp_job_id:
            raise RuntimeError(f"job create returned no id: {str(job)[:150]}")
        return

    # STEP 3 — poll the job; on finish, download the render
    job = tensorpix.get_job(row.tp_job_id)
    status = job.get("status")
    if status == tensorpix.FINISHED:
        url = tensorpix.job_output_url(job)
        if not url:                      # done but URL not attached yet — retry
            return
        if row.uniquify and video_freshen.available():
            # download the enhanced render, then apply slowdown+colour+audio
            dl = out_dir / f"{row.id}_tp_{row.file_name}"
            tensorpix.download(url, str(dl))
            try:
                video_freshen.uniquify(str(dl), str(out),
                                       intensity=row.freshen_intensity or "medium",
                                       seed=row.id)
            finally:
                try:
                    os.unlink(dl)
                except OSError:
                    pass
        else:
            tensorpix.download(url, str(out))
        _store(db, models, row, out)
    elif status in (tensorpix.FAILED, tensorpix.CANCELLED):
        row.status = "error"
        row.error = (job.get("error") or job.get("status_display")
                     or f"TensorPix job {row.tp_job_id} did not finish (status {status}).")
    # else QUEUED/PROCESSING → wait for the next hop


def _gc_source(db, models, source_md5: str) -> None:
    """Delete the shared source file once every variant of it is done."""
    if not source_md5:
        return
    db.flush()   # autoflush is off — make this row's new status visible to the count
    remaining = (db.query(models.Creative)
                 .filter(models.Creative.source_md5 == source_md5,
                         models.Creative.status == "processing").count())
    if remaining:
        return
    row = (db.query(models.Creative)
           .filter(models.Creative.source_md5 == source_md5,
                   models.Creative.src_path != "").first())
    if row and row.src_path and os.path.exists(row.src_path):
        try:
            os.unlink(row.src_path)
        except OSError:
            pass


def pending_count(db) -> int:
    from . import models
    return (db.query(models.Creative)
            .filter(models.Creative.status == "processing").count())
