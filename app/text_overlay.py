"""Text on images, rendered with the typeface TikTok's app uses for captions.

The editor in the browser positions/sizes the text over a scaled preview using
the SAME font files (served from /static/fonts) and the SAME em-relative
geometry defined here, so the baked image matches the preview.

Spec (all positions/sizes are fractions of the image, so they're resolution-independent):
  text       the lines (\\n separated)
  x, y       centre of the text block, 0..1 of width / height
  size       line font size as a fraction of image HEIGHT (0.02..0.25)
  weight     regular | semibold | bold
  color      text colour (#rrggbb)
  style      plain   -> text with a soft shadow (TikTok's default look)
             box     -> per-line rounded background ("highlight" sticker)
             outline -> dark stroke around the glyphs
  box_color  background colour for `box`
  align      left | center | right
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"
WEIGHT_KEYS = ("tiktok", "regular", "semibold", "bold")
WEIGHT_LABELS = {"tiktok": "TikTok caption (default)", "regular": "Regular", "semibold": "SemiBold", "bold": "Bold"}

# The TikTok app's caption font is TikTok Sans (SIL OFL). Measured against a
# real caption from the app, side by side at the same x-height (glyph
# widths, stroke, outline and line pitch): optical size 36, width 116,
# weight 500, a dark outline of 0.08em and a 1.3em line pitch. The variable
# font carries every axis.
TIKTOK_VARIABLE = "TikTokSans-Variable.ttf"
TIKTOK_OPSZ, TIKTOK_WDTH = 36, 116
TIKTOK_WGHT = {"tiktok": 500, "regular": 400, "semibold": 600, "bold": 700}

FONTS: dict[str, dict] = {
    "tiktok_sans": {"label": "TikTok Sans — the app's caption font (measured match)", "css": "TikTok Sans Var"},
    "classic": {"label": "Custom font (your upload in Settings)", "css": "AdOps Classic", "custom": True},
}
STYLES = ("plain", "box", "outline")
ALIGNS = ("left", "center", "right")

# em-relative geometry — mirrored 1:1 by the CSS in the editor
LINE_HEIGHT = 1.3           # line box = 1.3em (the app's caption line pitch, measured: 35px per 27px em)
BOX_PAD_X = 0.50            # box padding left/right
BOX_PAD_Y = 0.22            # box padding top/bottom
BOX_RADIUS = 0.35
SHADOW_DY = 0.03
SHADOW_BLUR = 0.06
STROKE = 0.08               # outward stroke — the app's caption outline (measured against a side-by-side)
DEFAULT_SIZE = 0.035        # the app's default caption ≈ 3.5% of the image height
DEFAULT_STYLE = "outline"   # the app's caption look: white text, thin dark outline, no blur


def custom_font_dir() -> Path:
    from . import config
    return Path(config.DATA_DIR) / "fonts"


def custom_font_path(weight: str) -> Path | None:
    """The uploaded custom file for a weight (ttf or otf), if any."""
    d = custom_font_dir()
    for ext in ("ttf", "otf"):
        p = d / f"classic-{weight}.{ext}"
        if p.exists():
            return p
    return None


def custom_font_status() -> dict[str, bool]:
    return {w: custom_font_path(w) is not None for w in ("regular", "semibold", "bold")}


def font_bytes_ok(data: bytes) -> bool:
    """True when Pillow can load these bytes as a font."""
    import io
    try:
        ImageFont.truetype(io.BytesIO(data), 20)
        return True
    except Exception:  # noqa: BLE001
        return False


def available_fonts() -> list[dict]:
    """For the editor: [{key, label, css, ready}] — the custom slot is ready
    only once a file is uploaded."""
    out = []
    for key, f in FONTS.items():
        ready = any(custom_font_status().values()) if f.get("custom") else True
        out.append({"key": key, "label": f["label"], "css": f["css"], "ready": ready})
    return out


def default_font() -> str:
    return "tiktok_sans"


def tiktok_axes(weight: str) -> list:
    """[opsz, wdth, wght, slnt] for the variable font."""
    return [TIKTOK_OPSZ, TIKTOK_WDTH, TIKTOK_WGHT.get(weight, 500), 0]


def font_file(font: str, weight: str) -> Path:
    """The file behind (font, weight): the variable TikTok Sans, or the
    uploaded custom file (partial sets fall back to the nearest weight, and
    a missing custom set falls back to TikTok Sans)."""
    if font == "classic":
        w = "semibold" if weight == "tiktok" else weight
        order = {"regular": ("regular", "semibold", "bold"), "semibold": ("semibold", "bold", "regular"),
                 "bold": ("bold", "semibold", "regular")}[w]
        for cand in order:
            p = custom_font_path(cand)
            if p:
                return p
    return FONT_DIR / TIKTOK_VARIABLE


def load_font(font: str, weight: str, px: int) -> ImageFont.FreeTypeFont:
    path = font_file(font, weight)
    f = ImageFont.truetype(str(path), px)
    if path.name == TIKTOK_VARIABLE:
        f.set_variation_by_axes(tiktok_axes(weight))
    return f


def clean(spec: dict) -> dict:
    """Validate/clamp an editor spec."""
    text = str(spec.get("text") or "").replace("\r", "").strip("\n")
    lines = [ln.rstrip() for ln in text.split("\n")][:12]
    if not any(ln.strip() for ln in lines):
        raise ValueError("Type some text first.")

    def f(key, lo, hi, default):
        try:
            return min(max(float(spec.get(key, default)), lo), hi)
        except (TypeError, ValueError):
            return default
    weight = str(spec.get("weight") or "tiktok").lower()
    font = str(spec.get("font") or default_font()).lower()
    style = str(spec.get("style") or DEFAULT_STYLE).lower()
    align = str(spec.get("align") or "center").lower()
    return {
        "lines": lines, "x": f("x", 0.0, 1.0, 0.5), "y": f("y", 0.0, 1.0, 0.5),
        "size": f("size", 0.02, 0.25, DEFAULT_SIZE),
        "weight": weight if weight in WEIGHT_KEYS else "tiktok",
        "font": font if font in FONTS else default_font(),
        "style": style if style in STYLES else DEFAULT_STYLE,
        "align": align if align in ALIGNS else "center",
        "color": _hex(spec.get("color"), "#ffffff"),
        "box_color": _hex(spec.get("box_color"), "#000000"),
    }


def _hex(v, default: str) -> str:
    v = str(v or "").strip()
    if len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
        return v.lower()
    return default


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def render(image_path: str, spec: dict) -> bytes:
    """Return PNG bytes of the image with the text baked in at native resolution."""
    import io
    s = clean(spec)
    with Image.open(image_path) as im:
        base = im.convert("RGBA")
    W, H = base.size
    px = max(8, int(round(s["size"] * H)))
    font = load_font(s["font"], s["weight"], px)
    line_h = int(round(px * LINE_HEIGHT))
    # per-line widths (advance widths, like the browser's inline boxes)
    widths = [int(round(font.getlength(ln))) if ln else 0 for ln in s["lines"]]
    block_w, block_h = max(widths) if widths else 0, line_h * len(s["lines"])
    cx, cy = s["x"] * W, s["y"] * H
    top = cy - block_h / 2
    left = cx - block_w / 2

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    text_rgb = _rgb(s["color"])
    # vertical centring of glyphs inside a 1.15em line box, like CSS line-height
    ascent, descent = font.getmetrics()
    glyph_off = (line_h - (ascent + descent)) / 2

    def line_x(i: int) -> float:
        if s["align"] == "left":
            return left
        if s["align"] == "right":
            return left + block_w - widths[i]
        return cx - widths[i] / 2

    if s["style"] == "box":
        pad_x, pad_y, rad = px * BOX_PAD_X, px * BOX_PAD_Y, px * BOX_RADIUS
        box_rgb = _rgb(s["box_color"])
        for i, ln in enumerate(s["lines"]):
            if not ln.strip():
                continue
            x0 = line_x(i) - pad_x
            y0 = top + i * line_h - pad_y
            draw.rounded_rectangle([x0, y0, x0 + widths[i] + 2 * pad_x, y0 + line_h + 2 * pad_y],
                                   radius=rad, fill=box_rgb + (255,))
    elif s["style"] == "plain":
        # soft shadow: draw the text on its own layer, blur, then composite
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        for i, ln in enumerate(s["lines"]):
            sd.text((line_x(i), top + i * line_h + glyph_off + px * SHADOW_DY), ln,
                    font=font, fill=(0, 0, 0, 150))
        shadow = shadow.filter(ImageFilter.GaussianBlur(px * SHADOW_BLUR))
        layer = Image.alpha_composite(layer, shadow)
        draw = ImageDraw.Draw(layer)

    for i, ln in enumerate(s["lines"]):
        pos = (line_x(i), top + i * line_h + glyph_off)
        if s["style"] == "outline":
            draw.text(pos, ln, font=font, fill=text_rgb + (255,),
                      stroke_width=max(1, int(round(px * STROKE))), stroke_fill=(0, 0, 0, 255))
        else:
            draw.text(pos, ln, font=font, fill=text_rgb + (255,))

    out = Image.alpha_composite(base, layer)
    buf = io.BytesIO()
    # keep the original format's strengths: PNG for PNG sources, JPEG otherwise
    if str(image_path).lower().endswith((".jpg", ".jpeg")):
        out.convert("RGB").save(buf, "JPEG", quality=92)
    else:
        out.save(buf, "PNG", optimize=True)
    return buf.getvalue()
