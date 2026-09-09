"""Image "uniquify" — the picture counterpart of video_freshen: turn one image into N
copies that TikTok's fingerprinting reads as different files while a person sees the
same creative.

Levers (all seeded by the variant's row id, so re-runs are reproducible and N variants
of one source differ from each other):
  * a tiny crop (0.4–2 % per edge, asymmetric) scaled back to the ORIGINAL size —
    aspect ratio and on-image text stay put
  * a gentle colour grade: brightness / contrast / saturation each within ±(2–6) %
  * fine luminance grain (invisible at phone size, enough to move every pixel)
  * fresh encode (JPEG quality 90–94 / PNG) with all metadata dropped
Strength: light | medium | strong scales every lever.
"""
from __future__ import annotations

import io
import random

from PIL import Image, ImageEnhance

STRENGTH = {"light": 0.5, "medium": 1.0, "strong": 1.6}


def _rng(seed: int | None) -> random.Random:
    return random.Random(seed if seed is not None else random.randrange(1 << 30))


def uniquify(src_path: str, dst_path: str, intensity: str = "medium", seed: int | None = None) -> None:
    k = STRENGTH.get(intensity, 1.0)
    rng = _rng(seed)
    with Image.open(src_path) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        work = im.convert("RGBA" if has_alpha else "RGB")
        w, h = work.size
        # 1) asymmetric micro-crop, then back to the exact original size
        def edge():
            return rng.uniform(0.004, 0.02) * k
        l, t, r, b = int(w * edge()), int(h * edge()), int(w * edge()), int(h * edge())
        if w - l - r > 16 and h - t - b > 16:
            work = work.crop((l, t, w - r, h - b)).resize((w, h), Image.LANCZOS)
        # 2) colour grade (alpha untouched)
        rgb = work.convert("RGB") if has_alpha else work
        for enh in (ImageEnhance.Brightness, ImageEnhance.Contrast, ImageEnhance.Color):
            rgb = enh(rgb).enhance(1.0 + rng.uniform(-0.06, 0.06) * k)
        # 3) fine grain: a low-amplitude noise layer blended in
        noise = Image.effect_noise((w, h), 12 + 10 * k).convert("L")
        grain = Image.merge("RGB", (noise, noise, noise))
        rgb = Image.blend(rgb, grain, 0.02 + 0.02 * k)
        if has_alpha:
            rgb.putalpha(work.getchannel("A"))
        out = rgb
        # 4) fresh encode, no metadata
        low = dst_path.lower()
        if low.endswith((".jpg", ".jpeg")):
            out.convert("RGB").save(dst_path, "JPEG", quality=int(90 + rng.uniform(0, 4)), optimize=True, subsampling=0)
        elif low.endswith(".webp"):
            out.save(dst_path, "WEBP", quality=int(90 + rng.uniform(0, 4)), method=4)
        else:
            out.save(dst_path, "PNG", optimize=True)
