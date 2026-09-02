"""Deliver carousel slides in a size TikTok accepts.

TikTok rejects image uploads whose pixel size/aspect isn't one it supports
("Image size is not supported"). Its carousel spec lists exactly these:
    vertical   720 x 1280   (9:16)
    square     640 x 640    (1:1)
    horizontal 1200 x 628   (~1.91:1)
So before a slide is uploaded, a *delivery copy* is made in the closest of
those formats: a near-match aspect is simply resized; anything else is
centre-cropped to the target ratio first. The original stays untouched, and
the copy is cached next to it so it's made once.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

FORMATS = {                     # name -> (w, h)
    "vertical": (720, 1280),
    "square": (640, 640),
    "horizontal": (1200, 628),
}
NEAR = 0.03                     # ≤3% aspect difference → resize only, no crop


def pick_format(w: int, h: int) -> tuple[str, tuple[int, int]]:
    """Closest listed format by aspect ratio."""
    r = w / h
    best = min(FORMATS.items(), key=lambda kv: abs((kv[1][0] / kv[1][1]) - r))
    return best[0], best[1]


def carousel_ready(src_path: str, cache_dir: Path, key: str) -> tuple[str, str]:
    """Return (path_to_upload, format_name). The source is used as-is when it
    already IS one of the listed sizes; otherwise a cached JPEG copy is built."""
    with Image.open(src_path) as im:
        w, h = im.size
        name, (tw, th) = pick_format(w, h)
        if (w, h) == (tw, th):
            return src_path, name
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"{key}_{tw}x{th}.jpg"
        if out.exists():
            return str(out), name
        img = im.convert("RGB")
        r_src, r_tgt = w / h, tw / th
        if abs(r_src - r_tgt) / r_tgt > NEAR:
            # centre-crop to the target ratio
            if r_src > r_tgt:          # too wide → trim sides
                nw = int(round(h * r_tgt)); x0 = (w - nw) // 2
                img = img.crop((x0, 0, x0 + nw, h))
            else:                      # too tall → trim top/bottom
                nh = int(round(w / r_tgt)); y0 = (h - nh) // 2
                img = img.crop((0, y0, w, y0 + nh))
        img = img.resize((tw, th), Image.LANCZOS)
        img.save(out, "JPEG", quality=90, optimize=True)
        return str(out), name
