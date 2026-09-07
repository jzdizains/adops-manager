"""Local copy of TikTok's Audio Library for the carousel builder.

TikTok's carousel music search has no "show everything" (keyword / recommend /
liked / history / uploads only). The Audio Library listing (CREATIVE_ASSET
scene) does page through the whole catalogue, and TikTok recommends caching
it and refreshing monthly — so we do exactly that, then confirm which tracks
Carousel Ads accept by asking SEARCH_BY_MUSIC_ID in the CAROUSEL_ADS scene
(100 ids per call). Preview urls last 12 h and are refreshed on demand.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models, queries, tiktok_api

REFRESH_DAYS = 30
URL_TTL = timedelta(hours=11)
SETTING_AT, SETTING_STATUS = "music_library_synced_at", "music_library_status"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def browse_account(db: Session):
    return (db.query(models.AdAccount)
            .filter(models.AdAccount.enabled == True,                 # noqa: E712
                    models.AdAccount.access_token != "",
                    models.AdAccount.status != "ACCESS_LOST")
            .order_by(models.AdAccount.advertiser_name).first())


def _upsert(db: Session, m: dict, now: datetime, carousel_ok: bool | None = None) -> models.MusicTrack | None:
    mid = str(m.get("music_id") or "")
    if not mid:
        return None
    row = db.query(models.MusicTrack).filter_by(music_id=mid).first()
    if not row:
        row = models.MusicTrack(music_id=mid)
        db.add(row)
    row.name = m.get("name") or m.get("file_name") or row.name or ""
    if m.get("author"):
        row.author = m["author"]
    if m.get("style"):
        row.style = m["style"]
    if m.get("duration") is not None:
        try:
            row.duration = float(m["duration"])
        except (TypeError, ValueError):
            pass
    if m.get("cover_url"):
        row.cover_url = m["cover_url"]
    if m.get("url"):
        row.url, row.url_at = m["url"], now
    row.copyright = m.get("copyright") or row.copyright or ""
    srcs = m.get("sources") or []
    if srcs:
        row.sources = ",".join(srcs)
    if "liked" in m:
        row.liked = bool(m.get("liked"))
    if carousel_ok is not None:
        row.carousel_ok = carousel_ok
    row.synced_at = now
    return row


def sync(db: Session, should_stop=None, on_progress=None) -> dict:
    """Pull the whole Audio Library, then confirm carousel usability in
    batches. Returns counts; never raises for TikTok errors (they're in
    'errors' and the status setting)."""
    acct = browse_account(db)
    r = {"tracks": 0, "carousel_ok": 0, "pages": 0, "errors": [], "stopped": False}
    if not acct:
        r["errors"].append("no connected ad account to browse the music library with")
        return r
    now = _now()
    seen: list[str] = []
    page = 1
    while True:
        if should_stop and should_stop():
            r["stopped"] = True
            break
        try:
            data = tiktok_api.list_music_library(acct.access_token, acct.advertiser_id, page=page)
        except tiktok_api.TikTokError as e:
            r["errors"].append(f"library page {page}: {e}")
            break
        musics = data.get("musics") or []
        for m in musics:
            row = _upsert(db, m, now)
            if row is not None:
                seen.append(row.music_id)
        r["pages"] = page
        db.commit()
        info = data.get("page_info") or {}
        total_page = int(info.get("total_page") or 1)
        if on_progress:
            on_progress(f"library page {page} of {total_page} · {len(seen)} tracks")
        if page >= total_page or not musics or page * tiktok_api.MUSIC_LIBRARY_PAGE_SIZE >= 100_000:
            break
        page += 1
    r["tracks"] = len(seen)
    # which of these can a Carousel Ad actually use? ask the carousel scene by id
    ok_ids: set[str] = set()
    for i in range(0, len(seen), tiktok_api.MUSIC_ID_BATCH):
        if should_stop and should_stop():
            r["stopped"] = True
            break
        chunk = seen[i:i + tiktok_api.MUSIC_ID_BATCH]
        try:
            found = tiktok_api.carousel_music_by_ids(acct.access_token, acct.advertiser_id, chunk)
        except tiktok_api.TikTokError as e:
            r["errors"].append(f"carousel check {i // tiktok_api.MUSIC_ID_BATCH + 1}: {e}")
            continue
        for m in found:
            row = _upsert(db, m, now, carousel_ok=True)
            if row is not None:
                ok_ids.add(row.music_id)
        for mid in chunk:
            if mid not in ok_ids:
                row = db.query(models.MusicTrack).filter_by(music_id=mid).first()
                if row is not None:
                    row.carousel_ok = False
        db.commit()
        if on_progress:
            on_progress(f"checking carousel usability {min(i + len(chunk), len(seen))} of {len(seen)} · {len(ok_ids)} usable")
    r["carousel_ok"] = len(ok_ids)
    if not r["stopped"] and seen:
        # tracks TikTok no longer lists
        gone = db.query(models.MusicTrack).filter(~models.MusicTrack.music_id.in_(seen)).all()
        for row in gone:
            db.delete(row)
        queries.set_setting(db, SETTING_AT, now.isoformat(timespec="seconds"))
    queries.set_setting(db, SETTING_STATUS, (
        f"{r['carousel_ok']} of {r['tracks']} tracks usable in carousels"
        + (" (stopped early)" if r["stopped"] else "")
        + (f" — {len(r['errors'])} error(s): {r['errors'][0][:120]}" if r["errors"] else "")))
    db.commit()
    return r


def synced_at(db: Session) -> datetime | None:
    v = queries.get_setting(db, SETTING_AT, "")
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def due(db: Session) -> bool:
    at = synced_at(db)
    return at is None or _now() - at > timedelta(days=REFRESH_DAYS)


def styles(db: Session) -> list[tuple[str, int]]:
    from sqlalchemy import func
    q = (db.query(models.MusicTrack.style, func.count(models.MusicTrack.id))
         .filter(models.MusicTrack.carousel_ok == True, models.MusicTrack.style != "")  # noqa: E712
         .group_by(models.MusicTrack.style).order_by(models.MusicTrack.style).all())
    return [(s_, n) for s_, n in q]


def browse(db: Session, q: str = "", style: str = "", page: int = 1, size: int = 60,
           sort: str = "name") -> dict:
    """Carousel-usable tracks from the cache, filtered and paged."""
    query = db.query(models.MusicTrack).filter(models.MusicTrack.carousel_ok == True)  # noqa: E712
    if style:
        query = query.filter(models.MusicTrack.style == style)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(models.MusicTrack.name.ilike(like) | models.MusicTrack.author.ilike(like))
    total = query.count()
    if sort == "duration":
        query = query.order_by(models.MusicTrack.duration, models.MusicTrack.name)
    else:
        query = query.order_by(models.MusicTrack.name)
    size = max(1, min(size, 200))
    page = max(1, page)
    rows = query.offset((page - 1) * size).limit(size).all()
    return {"rows": rows, "total": total, "page": page, "pages": max(1, -(-total // size))}


def fresh_urls(db: Session, rows: list[models.MusicTrack]) -> None:
    """Preview urls expire after 12 h — re-fetch the stale ones for this page."""
    stale = [r for r in rows if not r.url or not r.url_at or _now() - r.url_at > URL_TTL]
    if not stale:
        return
    acct = browse_account(db)
    if not acct:
        return
    try:
        found = tiktok_api.carousel_music_by_ids(acct.access_token, acct.advertiser_id, [r.music_id for r in stale])
    except tiktok_api.TikTokError:
        return
    now = _now()
    for m in found:
        _upsert(db, m, now, carousel_ok=True)
    db.commit()


def as_json(row: models.MusicTrack) -> dict:
    return {"music_id": row.music_id, "name": row.name, "author": row.author, "duration": row.duration,
            "url": row.url or "", "cover_url": row.cover_url or "", "style": row.style or "",
            "sources": (row.sources or "").split(",") if row.sources else [], "liked": bool(row.liked),
            "copyright": row.copyright or ""}
