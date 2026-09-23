"""Captions burned into a video — the Creatives drawer's "Aa Caption" (Studio).

The caption is drawn by the SAME renderer as text on images (text_overlay: TikTok Sans with
the app's outline / box / shadow looks), onto a transparent layer the size of the video, and
ffmpeg overlays it — for the whole clip, or between a start and an end second. The result is
a NEW library video (the original is never touched): same family (source_md5), so its results
group with the original's, and a fresh file for TikTok.

The preview is one frame of the video with the caption drawn by the same code, so what you see
is what gets burned in. Rendering runs as a background job (slow lane), one at a time.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading

from . import video_freshen

TIMEOUT_S = 600
_LOCK = threading.Lock()


def clean_times(start, end, duration: float | None = None) -> tuple[float, float | None]:
    """(start, end or None for 'to the end'), clamped and ordered. Pure."""
    def num(v):
        try:
            return max(float(v), 0.0)
        except (TypeError, ValueError):
            return None
    s = num(start) or 0.0
    e = num(end)
    if duration:
        s = min(s, max(duration - 0.1, 0.0))
        e = None if e is None or e >= duration else e
    if e is not None and e <= s:
        e = None
    return round(s, 2), (round(e, 2) if e is not None else None)


def overlay_filter(start: float, end: float | None) -> str:
    """The ffmpeg filter graph: the caption layer over the video, only in the window. Pure."""
    if start <= 0 and end is None:
        return "[0:v][1:v]overlay=0:0:format=auto[v]"
    upper = f"{end}" if end is not None else "1e9"
    return f"[0:v][1:v]overlay=0:0:format=auto:enable='between(t,{start},{upper})'[v]"


def overlay_png(width: int, height: int, spec: dict) -> bytes:
    """The caption alone on a transparent layer of the video's size (PNG, keeps alpha)."""
    from PIL import Image
    from . import text_overlay
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        blank = fh.name
    try:
        Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 0)).save(blank, "PNG")
        return text_overlay.render(blank, spec)
    finally:
        try:
            os.unlink(blank)
        except OSError:
            pass


def frame(src: str, at: float = 1.0) -> bytes:
    """One JPEG frame of the video at `at` seconds (for the preview)."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
        out = fh.name
    try:
        cmd = [video_freshen.ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", str(max(at, 0.0)),
               "-i", src, "-frames:v", "1", "-q:v", "3", out]
        p = subprocess.run(cmd, capture_output=True, timeout=60)
        if p.returncode != 0 or not os.path.getsize(out):
            raise RuntimeError("couldn't read a frame from the video")
        with open(out, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def preview(src: str, spec: dict, at: float = 1.0, max_width: int = 540) -> bytes:
    """The frame with the caption drawn by the real renderer, downscaled for the editor."""
    from . import text_overlay
    data = frame(src, at)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        return text_overlay.render(path, spec, max_width=max_width, quality=82)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def dims_from_banner(text: str) -> tuple[int, int] | None:
    """(w, h) from `ffmpeg -i` output ("Stream #0:0…: Video: h264 …, 1080x1920 [SAR…]"), honouring
    a 90/270° rotation tag. Pure."""
    import re
    m = re.search(r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})", text or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    rot = re.search(r"rotat(?:e|ion)(?:\s*[:=]|\s+of)\s*(-?\d+)", text or "")
    if rot and abs(int(rot.group(1))) % 180 == 90:
        w, h = h, w
    return w, h


def dimensions(src: str) -> tuple[int, int] | None:
    """The size the video is SHOWN at, from ffmpeg's own banner (works with the bundled ffmpeg —
    Render's native runtime has no ffprobe — and honours a rotation tag); ffprobe as a fallback."""
    try:            # the banner first: it carries the rotation a phone video is shown with
        p = subprocess.run([video_freshen.ffmpeg_exe(), "-hide_banner", "-i", src], capture_output=True, timeout=30)
        d = dims_from_banner((p.stderr or b"").decode("utf-8", "replace"))
        if d:
            return d
    except Exception:  # noqa: BLE001
        pass
    return video_freshen._dimensions(src)


def burn(src: str, dst: str, spec: dict, start: float = 0.0, end: float | None = None, timeout: int = TIMEOUT_S) -> None:
    """src → dst with the caption burned in (x264 CRF 18, audio copied). Raises RuntimeError."""
    dims = dimensions(src)
    if not dims:
        raise RuntimeError("couldn't read the video's size")
    png = overlay_png(dims[0], dims[1], spec)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(png)
        layer = fh.name
    tmp = dst + ".part"
    cmd = [video_freshen.ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-i", layer, "-filter_complex", overlay_filter(start, end),
           "-map", "[v]", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", tmp]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s")
    finally:
        try:
            os.unlink(layer)
        except OSError:
            pass
    if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("ffmpeg failed: " + (p.stderr or b"").decode("utf-8", "replace")[-300:])
    os.replace(tmp, dst)


def process(db, models, creative_id: int) -> dict:
    """Job body: render one queued captioned copy (status 'processing', text_spec holds
    {spec, start, end}, text_parent_id = the original)."""
    row = db.get(models.Creative, int(creative_id))
    if row is None or row.status != "processing":
        return {"ok": False, "detail": "nothing to render (deleted, or already done)"}
    parent = db.get(models.Creative, row.text_parent_id) if row.text_parent_id else None
    try:
        recipe = json.loads(row.text_spec or "{}")
    except ValueError:
        recipe = {}
    name, out = row.name, None
    with _LOCK:
        try:
            if parent is None or not parent.file_path or not os.path.exists(parent.file_path):
                raise RuntimeError("the original video is gone")
            if not video_freshen.available():
                raise RuntimeError("no ffmpeg on the server")
            out = os.path.join(os.path.dirname(parent.file_path), f"{row.id}_{row.file_name}")
            burn(parent.file_path, out, recipe.get("spec") or {}, float(recipe.get("start") or 0), recipe.get("end"))
            row.file_path, row.md5 = out, video_freshen._md5_file(out)
            row.size_bytes, row.status, row.error = os.path.getsize(out), "available", ""
            db.commit()
            return {"ok": True, "detail": f"“{row.name}” is ready", "href": "/creatives?view=library"}
        except Exception as e:      # noqa: BLE001 — recorded on the tile, never raised into the worker
            db.rollback()                    # the failure may BE a commit — start clean
            try:
                if out and os.path.exists(out):
                    os.unlink(out)           # the DB never took it: don't leave an orphan render on disk
            except OSError:
                pass
            try:
                fresh = db.get(models.Creative, int(creative_id))
                if fresh is not None:
                    fresh.status, fresh.error = "error", str(e)[:400]
                    db.commit()
            except Exception:      # noqa: BLE001
                db.rollback()
            return {"ok": False, "detail": f"“{name}”: {str(e)[:200]}", "href": "/creatives?view=library"}


def recover(db, models, older_than_min: int = 30) -> int:
    """Captioned copies stuck in 'processing' after a restart → error with a clear reason."""
    from datetime import datetime, timedelta
    cut = datetime.utcnow() - timedelta(minutes=older_than_min)
    # a burn still WAITING in the queue survives a restart (only running jobs are failed) — leave it
    waiting = set()
    for j in db.query(models.Job).filter(models.Job.kind == "video_caption", models.Job.status == "queued"):
        try:
            waiting.add(int(json.loads(j.payload or "{}").get("creative_id") or 0))
        except (ValueError, TypeError, AttributeError):
            pass
    n = 0
    for r in (db.query(models.Creative).filter(models.Creative.status == "processing", models.Creative.kind == "video",
                                               models.Creative.text_parent_id.isnot(None), models.Creative.uploaded_at < cut)):
        if r.id in waiting:
            continue
        r.status, r.error = "error", "Interrupted — the server restarted while the caption was being burned in. Delete it and add the caption again."
        n += 1
    if n:
        db.commit()
    return n
