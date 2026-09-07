"""Text on images, rendered with the typeface TikTok's app uses for captions.

The editor in the browser positions/sizes the text over a scaled preview using
the SAME font files (served from /static/fonts) and the SAME em-relative
geometry defined here, so the baked image matches the preview.

Spec (all positions/sizes are fractions of the image, so they're resolution-independent):
  text       the lines (\\n separated)
  x, y       centre of the text block, 0..1 of width / height
  size       line font size as a fraction of image HEIGHT (0.02..0.25)
  weight     tiktok (the app's caption weight) | regular | semibold | bold
  color      text colour (#rrggbb)
  style      plain   -> text with a soft shadow (TikTok's default look)
             box     -> per-line rounded background ("highlight" sticker)
             outline -> dark stroke around the glyphs
  box_color  background colour for `box`
  align      left | center | right
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"
WEIGHT_KEYS = ("tiktok", "regular", "semibold", "bold")
WEIGHT_LABELS = {"tiktok": "TikTok caption (default)", "regular": "Regular", "semibold": "SemiBold", "bold": "Bold"}

# The TikTok app's caption font is TikTok Sans (SIL OFL), at the font's
# default width, weight 550 (between Medium and SemiBold), with a dark
# outline of 0.07em and a 1.2em line pitch — fitted against a real app
# caption rendered through this pipeline (glyph widths, x-height, stem and
# outline thickness, line pitch all within a few percent).
# iOS picks the optical-size axis from the point size automatically, so the
# bake does the same: opsz = the em in phone points (see render()).
TIKTOK_VARIABLE = "TikTokSans-Variable.ttf"
TIKTOK_WDTH = 100
TIKTOK_OPSZ = 24                      # fallback when the size isn't known (≈ a 24pt caption)
TIKTOK_OPSZ_RANGE = (12, 36)          # the axis range in the font
PHONE_POINTS_WIDE = 430               # an iPhone canvas is 430pt (Pro Max) / 393pt wide; TikTok's text is laid out in points
TIKTOK_WGHT = {"tiktok": 550, "regular": 400, "semibold": 600, "bold": 700}

FONTS: dict[str, dict] = {
    "tiktok_sans": {"label": "TikTok Sans — the app's caption font (measured match)", "css": "TikTok Sans Var"},
    "classic": {"label": "Custom font (your upload in Settings)", "css": "AdOps Classic", "custom": True},
}
STYLES = ("plain", "box", "outline")
ALIGNS = ("left", "center", "right")

# em-relative geometry — mirrored 1:1 by the CSS in the editor
LINE_HEIGHT = 1.2           # line box = 1.2em (the app's caption line pitch: 35px per 29px em, measured)
BOX_PAD_X = 0.50            # box padding left/right
BOX_PAD_Y = 0.22            # box padding top/bottom
BOX_RADIUS = 0.35
SHADOW_DY = 0.03
SHADOW_BLUR = 0.06
STROKE = 0.07               # outward stroke — the app's caption outline (fitted from pixel profiles)
DEFAULT_SIZE = 0.035        # the app's default caption ≈ 3.5% of the image height
DEFAULT_STYLE = "outline"   # the app's caption look: white text, thin dark outline, no blur

# Emoji: TikTok Sans has no emoji glyphs (and Pillow has no font fallback), so
# emoji runs are drawn with Noto Color Emoji (SIL OFL) — a bitmap colour font
# with one 109px strike, rendered there and scaled to the caption size. Emoji
# get no outline/shadow, like the app.
EMOJI_FONT = "NotoColorEmoji.ttf"
EMOJI_STRIKE = 109
EMOJI_RE = re.compile(
    "(?:"
    "[\U0001F1E6-\U0001F1FF]{2}"                                   # flags (regional indicator pairs)
    "|[0-9#*]\uFE0F?\u20E3"                                          # keycaps
    "|(?:[\u00A9\u00AE\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u231A\u231B\u2328\u23CF"
    "\u23E9-\u23F3\u23F8-\u23FA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2600-\u27BF\u2934\u2935"
    "\u2B05-\u2B07\u2B1B\u2B1C\u2B50\u2B55\u3030\u303D\u3297\u3299\U0001F000-\U0001FAFF]"
    "(?:[\U0001F3FB-\U0001F3FF]|\uFE0F|\uFE0E)?"                     # skin tone / presentation selector
    "(?:\U000E0020-\U000E007F)*"                                     # tag sequences (subdivision flags)
    "(?:\u200D[\u2600-\u27BF\U0001F000-\U0001FAFF](?:[\U0001F3FB-\U0001F3FF]|\uFE0F)?)*)"   # ZWJ sequences
    ")")
_emoji_font: ImageFont.FreeTypeFont | None = None
_emoji_cache: dict[str, Image.Image] = {}


def emoji_font() -> ImageFont.FreeTypeFont | None:
    global _emoji_font
    if _emoji_font is None:
        try:
            _emoji_font = ImageFont.truetype(str(FONT_DIR / EMOJI_FONT), EMOJI_STRIKE)
        except OSError:
            _emoji_font = False  # type: ignore[assignment]
    return _emoji_font or None


def emoji_bitmap(seq: str) -> Image.Image | None:
    """The emoji sequence rendered at the font's native strike, cropped to its
    ink (RGBA), or None when the font has no glyph for it."""
    if seq in _emoji_cache:
        return _emoji_cache[seq]
    f = emoji_font()
    out = None
    if f is not None:
        try:
            w = int(f.getlength(seq)) + 8
            im = Image.new("RGBA", (max(w, 8), EMOJI_STRIKE + 40), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((4, 4), seq, font=f, embedded_color=True)
            box = im.getbbox()
            # a "missing glyph" box renders as a thin outline; real emoji ink is dense
            if box and (box[2] - box[0]) > 10:
                out = im.crop(box)
        except (OSError, ValueError):
            out = None
    _emoji_cache[seq] = out
    return out


def segments(line: str) -> list[tuple[str, str]]:
    """Split a line into ("text", run) / ("emoji", sequence) pieces."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in EMOJI_RE.finditer(line):
        if m.start() > pos:
            out.append(("text", line[pos:m.start()]))
        if emoji_bitmap(m.group(0)) is not None:
            out.append(("emoji", m.group(0)))
        else:
            out.append(("text", m.group(0)))       # no colour glyph → let the text font try
        pos = m.end()
    if pos < len(line):
        out.append(("text", line[pos:]))
    # merge adjacent text runs (a non-emoji match next to text)
    merged: list[tuple[str, str]] = []
    for kind, val in out:
        if merged and kind == "text" and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + val)
        else:
            merged.append((kind, val))
    return merged


def _emoji_size(px: int) -> float:
    return px / EMOJI_STRIKE          # the strike is drawn for a 109px em → scale to ours


def measure_line(line: str, font: ImageFont.FreeTypeFont, px: int) -> float:
    """Advance width of a line: text by the font, emoji by their bitmaps."""
    total = 0.0
    k = _emoji_size(px)
    for kind, val in segments(line):
        if kind == "text":
            total += font.getlength(val)
        else:
            bm = emoji_bitmap(val)
            total += (bm.width * k if bm else 0) + px * EMOJI_GAP
    return total


EMOJI_GAP = 0.06            # breathing room after an emoji (em)


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


def tiktok_opsz(px: float, image_width: int) -> int:
    """The optical size iOS would apply: the em measured in phone points
    (an image as wide as the phone screen → px / (W / 430pt)), clamped to
    the font's 12–36 axis."""
    if not px or not image_width:
        return TIKTOK_OPSZ
    lo, hi = TIKTOK_OPSZ_RANGE
    return int(round(min(hi, max(lo, px * PHONE_POINTS_WIDE / image_width))))


def tiktok_axes(weight: str, opsz: int | None = None) -> list:
    """[opsz, wdth, wght, slnt] for the variable font."""
    return [opsz if opsz is not None else TIKTOK_OPSZ, TIKTOK_WDTH, TIKTOK_WGHT.get(weight, 550), 0]


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


def load_font(font: str, weight: str, px: int, opsz: int | None = None) -> ImageFont.FreeTypeFont:
    path = font_file(font, weight)
    f = ImageFont.truetype(str(path), px)
    if path.name == TIKTOK_VARIABLE:
        f.set_variation_by_axes(tiktok_axes(weight, opsz))
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


def _stroke_text(draw, pos, text, font, fill, width: float) -> None:
    """Outlined text with a fractional stroke width where Pillow supports it
    (12+), else the nearest whole pixel — the same call either way."""
    try:
        draw.text(pos, text, font=font, fill=fill, stroke_width=max(1.0, width), stroke_fill=(0, 0, 0, 255))
    except TypeError:
        draw.text(pos, text, font=font, fill=fill, stroke_width=max(1, int(round(width))), stroke_fill=(0, 0, 0, 255))


def _draw_line(draw, layer: Image.Image, pos, line: str, font: ImageFont.FreeTypeFont, px: int,
               fill, stroke: float, emoji: bool) -> None:
    """One line: text runs with the caption font (outlined when stroke > 0),
    emoji runs as colour bitmaps scaled to the em and sat on the baseline."""
    x, y = pos
    ascent, _descent = font.getmetrics()
    k = _emoji_size(px)
    for kind, val in segments(line):
        if kind == "text":
            if stroke:
                _stroke_text(draw, (x, y), val, font, fill, stroke)
            else:
                draw.text((x, y), val, font=font, fill=fill)
            x += font.getlength(val)
            continue
        bm = emoji_bitmap(val)
        if bm is None:
            continue
        w, h = max(1, round(bm.width * k)), max(1, round(bm.height * k))
        if emoji:
            glyph = bm.resize((w, h), Image.LANCZOS)
            # the strike's ink sits roughly on the baseline with the same
            # ascent as the text: align its bottom with the baseline + a bit
            ey = int(round(y + ascent - h + px * 0.12))
            layer.alpha_composite(glyph, (int(round(x)), max(0, ey)))
        x += w + px * EMOJI_GAP


def render(image_path: str, spec: dict, max_width: int | None = None, quality: int = 92) -> bytes:
    """Return the image with the text baked in at native resolution (PNG for
    PNG sources, JPEG otherwise). max_width: also downscale the RESULT for a
    preview — the text is still rendered at native size first, so the preview
    shows exactly what the saved file will look like."""
    import io
    s = clean(spec)
    with Image.open(image_path) as im:
        base = im.convert("RGBA")
    W, H = base.size
    px = max(8, int(round(s["size"] * H)))
    font = load_font(s["font"], s["weight"], px, tiktok_opsz(px, W))
    line_h = int(round(px * LINE_HEIGHT))
    # per-line widths (advance widths, like the browser's inline boxes; emoji by bitmap)
    widths = [int(round(measure_line(ln, font, px))) if ln else 0 for ln in s["lines"]]
    block_w, block_h = max(widths) if widths else 0, line_h * len(s["lines"])
    cx, cy = s["x"] * W, s["y"] * H
    top = cy - block_h / 2
    left = cx - block_w / 2

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    text_rgb = _rgb(s["color"])
    # vertical centring of glyphs inside the line box, like CSS line-height
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
            _draw_line(sd, shadow, (line_x(i), top + i * line_h + glyph_off + px * SHADOW_DY), ln,
                       font, px, (0, 0, 0, 150), stroke=0, emoji=False)
        shadow = shadow.filter(ImageFilter.GaussianBlur(px * SHADOW_BLUR))
        layer = Image.alpha_composite(layer, shadow)
        draw = ImageDraw.Draw(layer)

    for i, ln in enumerate(s["lines"]):
        pos = (line_x(i), top + i * line_h + glyph_off)
        _draw_line(draw, layer, pos, ln, font, px, text_rgb + (255,),
                   stroke=(px * STROKE if s["style"] == "outline" else 0), emoji=True)

    out = Image.alpha_composite(base, layer)
    buf = io.BytesIO()
    if max_width and out.width > max_width:
        out = out.resize((max_width, max(1, round(out.height * max_width / out.width))), Image.LANCZOS)
        out.convert("RGB").save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    # keep the original format's strengths: PNG for PNG sources, JPEG otherwise
    if str(image_path).lower().endswith((".jpg", ".jpeg")):
        out.convert("RGB").save(buf, "JPEG", quality=quality)
    else:
        out.save(buf, "PNG", optimize=True)
    return buf.getvalue()
