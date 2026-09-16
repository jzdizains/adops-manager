"""Fit a video to TikTok's minimum size before it is uploaded for an ad.

Seen live (Diagnostics, 16 Sep): a small video uploads with flaw ILLEGAL_VIDEO_SIZE +
LOW_RESOLUTION, then /ad/create/ refuses with "Unsupported image size" — the ad's
auto-generated cover is as small as the video. TikTok's own auto-fix runs as a
background task the launch can't wait for, so the launcher fixes the file itself:
a delivery copy scaled up (Lanczos) to the standard frame for its orientation,
letterboxed when the aspect ratio doesn't match, encoded once and kept next to the
original so retries and other accounts reuse it.

Minimums (TikTok in-feed video spec): vertical 540×960, square 640×640, horizontal
960×540. Targets: 1080×1920, 1080×1080, 1920×1080.
"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger("adops.video_fit")

MIN = {"vertical": (540, 960), "square": (640, 640), "horizontal": (960, 540)}
TARGET = {"vertical": (1080, 1920), "square": (1080, 1080), "horizontal": (1920, 1080)}
SUFFIX = ".ttfit.mp4"


def orientation(w: int, h: int) -> str:
    if h > w * 1.05:
        return "vertical"
    if w > h * 1.05:
        return "horizontal"
    return "square"


def too_small(w: int, h: int) -> bool:
    mw, mh = MIN[orientation(w, h)]
    return w < mw or h < mh


def target_for(w: int, h: int) -> tuple[int, int] | None:
    """The frame to deliver at, or None when the file already meets the minimum."""
    if w <= 0 or h <= 0 or not too_small(w, h):
        return None
    return TARGET[orientation(w, h)]


def delivery_path(src_path: str) -> str:
    return src_path + SUFFIX


def fit(src_path: str, timeout: int = 420) -> tuple[str, tuple[int, int], tuple[int, int]] | None:
    """Return (path_to_upload, original_dims, delivered_dims) when the file needed
    scaling, None when it can be uploaded as it is (or its size can't be read).
    A copy made earlier is reused. Raises RuntimeError when ffmpeg fails."""
    from . import video_freshen
    dims = video_freshen._dimensions(src_path)
    if not dims:
        return None
    w, h = dims
    tgt = target_for(w, h)
    if tgt is None:
        return None
    out = delivery_path(src_path)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out, (w, h), tgt
    tw, th = tgt
    tmp = out + ".part"
    vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    cmd = [video_freshen.ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", src_path, "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", vf,
           # light on the 512 MB host: fast preset, 2 threads, short lookahead
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2",
           "-x264-params", "rc-lookahead=10", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-f", "mp4", tmp]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _unlink(tmp)
        raise RuntimeError(f"ffmpeg timed out after {timeout}s scaling {os.path.basename(src_path)}")
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _unlink(tmp)
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}): {tail}")
    os.replace(tmp, out)
    log.info("scaled %s %sx%s → %sx%s for TikTok", os.path.basename(src_path), w, h, tw, th)
    return out, (w, h), tgt


def _unlink(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass
