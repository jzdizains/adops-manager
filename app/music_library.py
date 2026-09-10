"""Local copy of TikTok's Audio Library for the carousel builder.

TikTok's carousel music search has no "show everything" (keyword / recommend /
liked / history / uploads only). The Audio Library listing (CREATIVE_ASSET
scene) does page through the whole catalogue, and TikTok recommends caching
it and refreshing monthly — so we do exactly that. Which tracks Carousel Ads
accept is marked in the listing itself (author / liked / cover_url are "returned
only for music that can be used in Carousel Ads"); those candidates are then
confirmed with SEARCH_BY_MUSIC_ID in the CAROUSEL_ADS scene (100 ids per call).
In practice the Audio Library is video music and the carousel picker lives on
TikTok's own shelves (recommend / keyword / liked / history / uploads) — the
cache is a bonus, never the only way. Preview urls last 12 h, refreshed on demand.
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


def _check_split(acct, ids: list[str], errors: list, depth: int = 0) -> list[dict]:
    """SEARCH_BY_MUSIC_ID rejects the WHOLE batch with 40000 "Invalid music id" when
    a single id is not valid for the carousel scene — which used to zero out the
    entire library. On that error split the batch in halves and retry; a lone bad
    id is simply left unusable."""
    if not ids:
        return []
    try:
        return tiktok_api.carousel_music_by_ids(acct.access_token, acct.advertiser_id, ids)
    except tiktok_api.TikTokError as e:
        if len(ids) == 1 or depth > 7:          # halving 100 reaches single ids at depth 7
            if len(ids) > 1 or len(errors) < 20:
                errors.append(f"carousel check ({len(ids)} id{'s' if len(ids) > 1 else ''}): {e}")
            return []
        mid = len(ids) // 2
        return _check_split(acct, ids[:mid], errors, depth + 1) + _check_split(acct, ids[mid:], errors, depth + 1)


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
    carousel_hint: set[str] = set()      # listing rows that carry author / cover_url / liked
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
                if m.get("author") or m.get("cover_url") or "liked" in m:
                    carousel_hint.add(row.music_id)
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
    # Which of these can a Carousel Ad actually use? The doc marks it in the listing itself:
    # author / liked / cover_url are "returned only for music that can be used in Carousel
    # Ads". Only those candidates are confirmed by id in the carousel scene (SEARCH_BY_MUSIC_ID
    # answers 40000 "Invalid music id" for anything else — asking about all 7,000+ library
    # tracks used to burn ~150 calls to learn that none qualify, and zero the library).
    ok_ids: set[str] = set()
    candidates = [m for m in seen if m in carousel_hint]
    r["candidates"] = len(candidates)
    db.query(models.MusicTrack).update({"carousel_ok": False}, synchronize_session=False)    # one statement, not one per track
    db.commit()
    db.expire_all()          # rows already loaded above must see the reset, or setting True again is a no-op
    empty_streak = 0
    for i in range(0, len(candidates), tiktok_api.MUSIC_ID_BATCH):
        if should_stop and should_stop():
            r["stopped"] = True
            break
        chunk = candidates[i:i + tiktok_api.MUSIC_ID_BATCH]
        found = _check_split(acct, chunk, r["errors"])
        for m in found:
            row = _upsert(db, m, now, carousel_ok=True)
            if row is not None:
                ok_ids.add(row.music_id)
        db.commit()
        empty_streak = 0 if found else empty_streak + 1
        if on_progress:
            on_progress(f"confirming carousel usability {min(i + len(chunk), len(candidates))} of {len(candidates)} · {len(ok_ids)} usable")
        if empty_streak >= 1 and not ok_ids:
            # a whole batch rejected and nothing usable yet: the carousel scene does not take
            # these ids at all — stop instead of asking about every remaining track
            r["errors"].append("carousel check stopped early: the first batch was rejected outright")
            break
    r["carousel_ok"] = len(ok_ids)
    if not r["stopped"] and seen:
        # tracks TikTok no longer lists
        gone = db.query(models.MusicTrack).filter(~models.MusicTrack.music_id.in_(seen)).all()
        for row in gone:
            db.delete(row)
        queries.set_setting(db, SETTING_AT, now.isoformat(timespec="seconds"))
    if r["tracks"] and not r["carousel_ok"] and not r["stopped"]:
        status = (f"{r['tracks']} tracks in TikTok's Audio Library, none of them accepted for Carousel Ads on this account "
                  "— carousel sounds come from “For your slides” and Search")
    else:
        status = f"{r['carousel_ok']} of {r['tracks']} tracks usable in carousels" + (" (stopped early)" if r["stopped"] else "")
    if r["errors"]:
        status += f" — {len(r['errors'])} error(s): {r['errors'][0][:120]}"
    queries.set_setting(db, SETTING_STATUS, status)
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
