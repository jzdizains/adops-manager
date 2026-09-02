"""Text on images, rendered with TikTok's own typeface (TikTok Sans, OFL).

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
WEIGHTS = {"regular": "TikTokSans-Regular.ttf", "semibold": "TikTokSans-SemiBold.ttf",
           "bold": "TikTokSans-Bold.ttf"}
STYLES = ("plain", "box", "outline")
ALIGNS = ("left", "center", "right")

# em-relative geometry — mirrored 1:1 by the CSS in the editor
LINE_HEIGHT = 1.15          # line box = 1.15em
BOX_PAD_X = 0.50            # box padding left/right
BOX_PAD_Y = 0.22            # box padding top/bottom
BOX_RADIUS = 0.35
SHADOW_DY = 0.03
SHADOW_BLUR = 0.06
STROKE = 0.08


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
    weight = str(spec.get("weight") or "semibold").lower()
    style = str(spec.get("style") or "plain").lower()
    align = str(spec.get("align") or "center").lower()
    return {
        "lines": lines, "x": f("x", 0.0, 1.0, 0.5), "y": f("y", 0.0, 1.0, 0.5),
        "size": f("size", 0.02, 0.25, 0.06),
        "weight": weight if weight in WEIGHTS else "semibold",
        "style": style if style in STYLES else "plain",
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
    font = ImageFont.truetype(str(FONT_DIR / WEIGHTS[s["weight"]]), px)
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
