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


def add(db: Session, data: bytes, file_name: str, name: str = "", owner_user_id: int | None = None) -> tuple[models.DisplayCard, bool]:
    """Store an uploaded card. Returns (row, was_resized)."""
    if len(data) > MAX_UPLOAD:
        raise ValueError("Image is over 15 MB.")
    png, orig, resized = fit(data)
    md5 = hashlib.md5(png).hexdigest()
    stem = _safe_name(file_name.rsplit(".", 1)[0] if "." in file_name else file_name)
    card = models.DisplayCard(name=(name or "").strip() or stem.replace("_", " "), md5=md5, owner_user_id=owner_user_id)
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
    from datetime import datetime as _dt
    if cached:
        cached.image_id, cached.portfolio_id, cached.upload_md5 = image_id, pid, card.md5
        cached.status, cached.error, cached.updated_at = "ok", "", _dt.utcnow()
    else:
        db.add(models.DisplayCardUpload(card_id=card.id, advertiser_id=acct.advertiser_id,
                                        image_id=image_id, portfolio_id=pid, upload_md5=card.md5,
                                        status="ok", updated_at=_dt.utcnow()))
    db.commit()
    return pid


# ---------------------------------------------------------------------------
# pushed ahead of launch (v147): every account of the workspace gets its copy now, with a
# status per account, instead of the first launch on each account doing the upload
# ---------------------------------------------------------------------------

def push_targets(db: Session, owner_user_id) -> list[models.AdAccount]:
    """Enabled, connected, delivering-capable accounts of the card's workspace."""
    q = db.query(models.AdAccount).filter(models.AdAccount.enabled == True)       # noqa: E712
    if owner_user_id is not None:
        from . import scope as _scope
        q = _scope.account_filter(db, q, owner_user_id)
    out = []
    for a in q.order_by(models.AdAccount.advertiser_name):
        st = str(a.status or "").upper()
        if a.access_token and (not st or "ENABLE" in st):
            out.append(a)
    return out


def push(db: Session, card: models.DisplayCard, accounts: list, should_stop=None, on_progress=None) -> dict:
    """Make sure every account has this card; record each outcome. Accounts that already
    have the current image are skipped without a call."""
    import time as _t
    from datetime import datetime as _dt
    done = skipped = 0
    failed: list[str] = []
    have = {u.advertiser_id: u for u in db.query(models.DisplayCardUpload).filter_by(card_id=card.id)}
    for i, acct in enumerate(accounts, 1):
        if should_stop and should_stop():
            break
        if on_progress and (i == 1 or i % 5 == 0 or i == len(accounts)):
            on_progress(f"{i} of {len(accounts)} accounts")
        u = have.get(acct.advertiser_id)
        if u is not None and u.portfolio_id and u.upload_md5 == card.md5:
            skipped += 1
            continue
        try:
            resolve_for_account(db, acct, card)
            done += 1
        except tiktok_api.TikTokError as e:
            db.rollback()
            row = db.query(models.DisplayCardUpload).filter_by(card_id=card.id, advertiser_id=acct.advertiser_id).first()
            if row is None:
                row = models.DisplayCardUpload(card_id=card.id, advertiser_id=acct.advertiser_id)
                db.add(row)
            row.status, row.error, row.updated_at = "failed", f"{e.code}: {e.message}"[:300], _dt.utcnow()
            row.portfolio_id = "" if row.upload_md5 != card.md5 else row.portfolio_id
            db.commit()
            failed.append(acct.advertiser_name or acct.advertiser_id)
        _t.sleep(0.2)
    return {"done": done, "skipped": skipped, "failed": failed}


def status(db: Session, card: models.DisplayCard, accounts: list) -> dict:
    """{ready, total, failed: [{account, error}], missing} over the given accounts."""
    ups = {u.advertiser_id: u for u in db.query(models.DisplayCardUpload).filter_by(card_id=card.id)}
    ready, failed, missing = 0, [], 0
    for a in accounts:
        u = ups.get(a.advertiser_id)
        if u is not None and u.portfolio_id and u.upload_md5 == card.md5:
            ready += 1
        elif u is not None and u.status == "failed":
            failed.append({"account": a.advertiser_name or a.advertiser_id, "advertiser_id": a.advertiser_id, "error": u.error or ""})
        else:
            missing += 1
    return {"ready": ready, "total": len(accounts), "failed": failed[:50], "n_failed": len(failed), "missing": missing}
