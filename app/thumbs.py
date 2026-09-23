"""Thumbnails we own.

TikTok's cover URLs are signed and die within hours, so a spark code's thumbnail went blank
the next day and a profile pick had none at all. This copies the cover while the URL is still
fresh: downloaded once, shrunk to a small JPEG under DATA_DIR/thumbs, served from our own
route with a long cache header.

Safety: only https URLs on TikTok's own CDN hosts are fetched (a cover URL can come from a
browser-posted pick, so anything else would let a client make the server fetch arbitrary
addresses), at most MAX_BYTES, one redirect re-checked against the same list, and decodes go
through a gate of two so a burst of picks can't balloon memory.
"""
from __future__ import annotations

import io
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from . import config

MAX_BYTES = 4 * 1024 * 1024
PX = 360                                  # long edge of the stored JPEG
CDN_SUFFIXES = (".tiktokcdn.com", ".tiktokcdn-us.com", ".tiktokcdn-eu.com", ".ibyteimg.com", ".byteimg.com",
                ".ibytedtos.com", ".muscdn.com", ".tiktokv.com", ".tiktokv.us", ".tiktokv.eu", ".tiktok.com")
_GATE = threading.BoundedSemaphore(2)
_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def thumbs_dir() -> Path:
    d = Path(config.DATA_DIR) / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def allowed(url: str) -> bool:
    """https on a TikTok CDN host — nothing else is ever fetched."""
    try:
        u = urlparse(str(url or ""))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and bool(host) and any(host.endswith(s) for s in CDN_SUFFIXES)


def path_for(kind: str, key) -> Path | None:
    """kind: 'sp' (spark code id) | 'pp' (profile post item id)."""
    k = str(key or "")
    if kind not in ("sp", "pp") or not _SAFE.match(k):
        return None
    return thumbs_dir() / f"{kind}_{k}.jpg"


def have(kind: str, key) -> bool:
    p = path_for(kind, key)
    return bool(p and p.exists() and p.stat().st_size > 0)


def url_for(kind: str, key) -> str:
    return f"/thumbs/{kind}/{key}.jpg"


def shrink(data: bytes) -> bytes:
    """Any image → a small RGB JPEG (long edge PX). Raises ValueError when it isn't one."""
    from PIL import Image, UnidentifiedImageError
    try:
        with _GATE:
            im = Image.open(io.BytesIO(data))
            im.draft("RGB", (PX * 2, PX * 2))            # JPEG: decode at reduced size
            im = im.convert("RGB")
            im.thumbnail((PX, PX))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82, optimize=True)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("not an image") from e


def _download(url: str, client=None) -> bytes:
    import httpx
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0), follow_redirects=False)
    try:
        for _ in range(2):                                   # the URL + one re-checked redirect
            if not allowed(url):
                raise ValueError("not a TikTok CDN address")
            with client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = r.headers.get("location", "")
                    continue
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code} (the signed URL has probably expired)")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise ValueError("too large")
                return bytes(buf)
        raise ValueError("too many redirects")
    finally:
        if own:
            client.close()


def cache(kind: str, key, url: str, client=None) -> tuple[bool, str]:
    """Download + store. (True, '') on success or when already stored; (False, why) otherwise.
    Never raises."""
    p = path_for(kind, key)
    if p is None:
        return False, "bad key"
    if have(kind, key):
        return True, ""
    try:
        data = shrink(_download(url, client))
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        return True, ""
    except Exception as e:      # noqa: BLE001 — a thumbnail must never break its caller
        return False, str(e)[:120]


def cache_spark(db, spark, url: str = "", client=None) -> bool:
    """Own a spark code's cover; on success its thumbnail_url becomes our route."""
    src = url or (spark.thumbnail_url or "")
    if have("sp", spark.id):
        if not (spark.thumbnail_url or "").startswith("/thumbs/"):
            spark.thumbnail_url = url_for("sp", spark.id)
        return True
    if not src or src.startswith("/"):
        return False
    ok, _ = cache("sp", spark.id, src, client)
    if ok:
        spark.thumbnail_url = url_for("sp", spark.id)
    return ok


_OWNED: dict[str, tuple[float, set]] = {}


def owned_keys(kind: str) -> set[str]:
    """Keys we hold a thumbnail for (one directory listing, reused for 30 s)."""
    import time as _t
    hit = _OWNED.get(kind)
    if hit and _t.time() - hit[0] < 30:
        return hit[1]
    pre = f"{kind}_"
    try:
        keys = {n[len(pre):-4] for n in (e.name for e in thumbs_dir().iterdir()) if n.startswith(pre) and n.endswith(".jpg")}
    except OSError:
        keys = set()
    _OWNED[kind] = (_t.time(), keys)
    return keys


def forget_owned() -> None:
    _OWNED.clear()


def cover_for_post(item_id: str, fresh_url: str = "") -> str:
    """The cover a profile post should show: ours when stored, else TikTok's (fresh) URL."""
    return url_for("pp", item_id) if have("pp", item_id) else (fresh_url or "")


def process_pending(db, models, limit: int = 20) -> int:
    """Background pass: spark codes still pointing at a TikTok URL get it copied while it may
    still be alive. One try per code (a dead URL is forgotten — the card shows the ✦ badge
    rather than a broken image)."""
    n = 0
    from sqlalchemy import or_
    rows = (db.query(models.SparkCode)
            .filter(or_(*[models.SparkCode.thumbnail_url.like(f"https://%{sfx}/%") for sfx in CDN_SUFFIXES]))
            .order_by(models.SparkCode.id.desc()).limit(limit).all())      # only TikTok's expiring URLs
    if not rows:
        return 0
    import httpx
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0), follow_redirects=False) as client:
        for s in rows:
            if not cache_spark(db, s, client=client):
                s.thumbnail_url = ""                   # expired: no broken image tomorrow
            n += 1
    db.commit()
    return n
