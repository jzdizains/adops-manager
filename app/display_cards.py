"""Display Cards — TikTok's in-feed image card (an interactive add-on that
acts as a second CTA). One upload here; each ad account gets its own copy:
image → /file/image/ad/upload/ → CARD portfolio → card_id on the ad.

TikTok requires exactly 750 × 421 px (doc "Cards → Display Card"), so an
upload of any other size is scaled to cover that box and centre-cropped.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from . import config, models, tiktok_api

W, H = tiktok_api.DISPLAY_CARD_SIZE
MAX_UPLOAD = 15 * 1024 * 1024


def cards_dir() -> Path:
    d = Path(config.DATA_DIR) / "display_cards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fit(data: bytes) -> tuple[bytes, tuple[int, int], bool]:
    """(png bytes at 750×421, original size, was_resized). Raises ValueError
    for anything Pillow can't open."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("That file isn't an image Pillow can read (use JPG or PNG).") from e
    orig = im.size
    im = im.convert("RGB")
    if im.size != (W, H):
        k = max(W / im.width, H / im.height)
        im = im.resize((max(W, round(im.width * k)), max(H, round(im.height * k))), Image.LANCZOS)
        left, top = (im.width - W) // 2, (im.height - H) // 2
        im = im.crop((left, top, left + W, top + H))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue(), orig, orig != (W, H)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:60] or "card"


def add(db: Session, data: bytes, file_name: str, name: str = "") -> tuple[models.DisplayCard, bool]:
    """Store an uploaded card. Returns (row, was_resized)."""
    if len(data) > MAX_UPLOAD:
        raise ValueError("Image is over 15 MB.")
    png, orig, resized = fit(data)
    md5 = hashlib.md5(png).hexdigest()
    stem = _safe_name(file_name.rsplit(".", 1)[0] if "." in file_name else file_name)
    card = models.DisplayCard(name=(name or "").strip() or stem.replace("_", " "), md5=md5)
    db.add(card)
    db.flush()
    path = cards_dir() / f"{card.id}_{stem}.png"
    path.write_bytes(png)
    card.file_name, card.file_path = path.name, str(path)
    db.commit()
    return card, resized


def remove(db: Session, card: models.DisplayCard) -> None:
    try:
        if card.file_path and Path(card.file_path).exists():
            Path(card.file_path).unlink()
    except OSError:
        pass
    db.query(models.DisplayCardUpload).filter_by(card_id=card.id).delete()
    db.delete(card)
    db.commit()


def resolve_for_account(db: Session, acct: models.AdAccount, card: models.DisplayCard) -> str:
    """This account's Display Card portfolio id for the card — uploading the
    image and creating the CARD portfolio the first time (cached by md5, so a
    re-uploaded/changed image gets a fresh portfolio). Raises TikTokError."""
    cached = (db.query(models.DisplayCardUpload)
              .filter_by(card_id=card.id, advertiser_id=acct.advertiser_id).first())
    if cached and cached.portfolio_id and cached.upload_md5 == card.md5:
        return cached.portfolio_id
    if not card.file_path or not Path(card.file_path).exists():
        raise tiktok_api.TikTokError("APP", f"Display card “{card.name}” file is missing — upload it again.")
    up = tiktok_api.upload_image_file(acct.access_token, acct.advertiser_id, card.file_path,
                                      f"card{card.id}_{card.file_name}"[:100])
    image_id = str(up.get("image_id") or "")
    if not image_id:
        raise tiktok_api.TikTokError("APP", "Display card image upload returned no image_id")
    pid = tiktok_api.create_display_card_portfolio(acct.access_token, acct.advertiser_id, image_id)
    if cached:
        cached.image_id, cached.portfolio_id, cached.upload_md5 = image_id, pid, card.md5
    else:
        db.add(models.DisplayCardUpload(card_id=card.id, advertiser_id=acct.advertiser_id,
                                        image_id=image_id, portfolio_id=pid, upload_md5=card.md5))
    db.commit()
    return pid
