"""Profile videos — every post of every TikTok profile a Business Center has access to,
so the Super Launcher can launch straight from a profile instead of a pasted spark code.

What TikTok gives us (official Marketing API, the OAuth token — no cookies):
  /identity/get/  identity_type=BC_AUTH_TT + identity_authorized_bc_id=<bc>
                  → the profiles the Business Center shares with this ad account
  /identity/video/get/  per profile → its ad-usable posts (item_id, caption, cover, type)
Both are asked through ONE enabled ad account of that Business Center; the answer is the
same for every account in it, which is why the picker is per BC.

Launching one of these posts is the existing spark path: the pick becomes a SparkCode row
(no auth code — `tiktok_item_id` is the key), and resolve_spark's "known item id → an
identity that lists it" step finds the profile on each target account.

Dashboard-safe: one BC's listing is a handful of API calls (per profile, 20 posts per
call, both post types, capped in tiktok_api). A listing is kept in the database (v147:
ProfileFetch + ProfilePost — survives restarts, served at once, re-read in the background
when older than CACHE_S) with a small in-memory copy in front (at most CACHE_MAX BCs).
Cover URLs TikTok hands out expire after about an hour, so a job copies them (thumbs.py)
right after every read; preview videos still need a fresh read (the Refresh button).
Nothing here is a credential.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from . import models, tiktok_api

CACHE_S = 600
CACHE_MAX = 30
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def _account_for_bc(db: Session, sc, bc_id: str) -> models.AdAccount | None:
    """One enabled account of the BC, in the viewer's workspace, that has a token."""
    q = (db.query(models.AdAccount).filter(models.AdAccount.enabled == True)  # noqa: E712
         .filter(models.AdAccount.owner_bc_id == bc_id).order_by(models.AdAccount.advertiser_name))
    for a in q:
        if sc.allows(a.advertiser_id) and a.access_token:
            return a
    return None


def _first_url(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def _created(info: dict, item_id: str) -> str:
    """TikTok's create time when it sends one (unix seconds or a date string), else the
    date baked into the post id itself."""
    raw = info.get("create_time") or info.get("created_at") or ""
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        try:
            from datetime import datetime as _dt
            return _dt.utcfromtimestamp(int(raw)).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            raw = ""
    if raw:
        return str(raw)[:19]
    from .review import item_date
    return item_date(item_id)


def _video(info: dict, handle: str) -> dict:
    """One post as the picker shows it. Cover and preview come from video_info
    (poster_url / preview_url, valid about an hour — the picker re-reads on Refresh)
    or, for a photo post, from the first image of carousel_info.image_info."""
    item_id = str(info.get("item_id") or "")
    kind = "carousel" if str(info.get("item_type", "")).upper() == "CAROUSEL" else "video"
    vi = info.get("video_info") if isinstance(info.get("video_info"), dict) else {}
    ci = info.get("carousel_info") if isinstance(info.get("carousel_info"), dict) else {}
    images = ci.get("image_info") if isinstance(ci.get("image_info"), list) else []
    first_img = images[0] if images and isinstance(images[0], dict) else {}
    cover = (_first_url(vi, "poster_url", "cover_url", "video_cover_url")
             or _first_url(info, "video_cover_url", "poster_url", "cover_url", "cover_image_url", "thumbnail_url")
             or _first_url(first_img, "image_url", "url", "web_uri"))
    preview = _first_url(vi, "preview_url", "url")
    duration = vi.get("duration") or info.get("duration") or 0
    return {
        "item_id": item_id,
        "text": (info.get("text") or "")[:200],
        "cover": cover,
        "preview": preview,
        "slides": len(images) if kind == "carousel" else 0,
        "type": kind,
        "auth_code": info.get("auth_code") or "",
        "url": info.get("share_url") or (f"https://www.tiktok.com/@{handle}/video/{item_id}" if handle and item_id else ""),
        "created": _created(info, item_id),
        "duration": duration,
    }


def fetch_bc(db: Session, sc, bc_id: str) -> dict:
    """Live read (no cache): the BC's profiles and their posts."""
    acct = _account_for_bc(db, sc, bc_id)
    if acct is None:
        return {"ok": False, "error": "No enabled, connected ad account in this Business Center to ask TikTok through.",
                "profiles": [], "bc_id": bc_id}
    try:
        idents = tiktok_api.list_identities(acct.access_token, acct.advertiser_id,
                                            identity_type="BC_AUTH_TT", identity_authorized_bc_id=bc_id)
    except tiktok_api.TikTokError as e:
        return {"ok": False, "error": f"TikTok refused the profile list ({e.code}): {e.message}", "profiles": [], "bc_id": bc_id}
    profiles: list[dict] = []
    for ident in idents:
        iid = str(ident.get("identity_id") or "")
        if not iid:
            continue
        handle = str(ident.get("display_name") or ident.get("identity_name") or ident.get("username") or "")
        prof = {"identity_id": iid, "identity_type": "BC_AUTH_TT", "bc_id": bc_id, "name": handle or iid,
                "avatar": ident.get("profile_image") or ident.get("avatar_icon") or "", "videos": [], "error": ""}
        seen: set[str] = set()
        try:
            data = tiktok_api.list_tt_videos(acct.access_token, acct.advertiser_id, iid, "BC_AUTH_TT",
                                             identity_authorized_bc_id=bc_id)     # every post, both types (cursor-paged inside)
        except tiktok_api.TikTokError as e:
            prof["error"] = f"{e.code}: {e.message[:80]}"
            data = {"list": []}
        for item in data.get("list", []):
            v = _video(item.get("item_info", item) if isinstance(item, dict) else {}, handle)
            if v["item_id"] and v["item_id"] not in seen:
                seen.add(v["item_id"])
                prof["videos"].append(v)
        profiles.append(prof)
    # v152: the same TikTok accounts' spark-code (AUTH_CODE) identities on this ad account — their
    # posts are merged into the matching profile (the Business Center identity wins on overlap)
    code_profiles: list[dict] = []
    try:
        code_idents = tiktok_api.list_identities(acct.access_token, acct.advertiser_id, identity_type="AUTH_CODE")[:CODE_MAX]
    except tiktok_api.TikTokError:
        code_idents = []
    for ident in code_idents:
        iid = str(ident.get("identity_id") or "")
        if not iid:
            continue
        handle = str(ident.get("display_name") or ident.get("identity_name") or ident.get("username") or "")
        cp = {"identity_id": iid, "identity_type": "AUTH_CODE", "bc_id": bc_id, "name": handle or iid,
              "avatar": ident.get("profile_image") or ident.get("avatar_icon") or "", "videos": [], "error": ""}
        try:
            data = tiktok_api.list_tt_videos(acct.access_token, acct.advertiser_id, iid, "AUTH_CODE")
        except tiktok_api.TikTokError as e:
            cp["error"] = f"{e.code}: {e.message[:80]}"
            data = {"list": []}
        for item in data.get("list", []):
            v = _video(item.get("item_info", item) if isinstance(item, dict) else {}, handle)
            if v["item_id"]:
                cp["videos"].append(v)
        if cp["videos"]:
            code_profiles.append(cp)
    profiles = merge_code(profiles, code_profiles)
    profiles.sort(key=lambda p: (-len(p["videos"]), p["name"].lower()))
    return {"ok": True, "bc_id": bc_id, "via": acct.advertiser_id, "profiles": profiles,
            "total": sum(len(p["videos"]) for p in profiles), "fetched_at": time.time()}


CODE_MAX = 30          # spark-code identities read per Business Center (each is one more listing)


def merge_code(bc_profiles: list[dict], code_profiles: list[dict]) -> list[dict]:
    """One profile per TikTok account (v152): a spark-code identity's posts join the Business-Center
    profile with the same handle; a post on both stays the BC one (the code-free launch path).
    A code identity with no BC twin becomes its own profile. Every post says how it launches:
    via "bc", or via "code" with that identity. Pure."""
    out = []
    by_handle: dict = {}
    for p in bc_profiles:
        p = {**p, "videos": [{**v, "via": v.get("via") or "bc"} for v in p.get("videos") or []]}
        out.append(p)
        by_handle.setdefault((p.get("name") or "").strip().lower(), p)
    for cp in code_profiles:
        vids = [{**v, "via": "code", "identity_id": cp["identity_id"], "identity_type": "AUTH_CODE"} for v in cp.get("videos") or []]
        twin = by_handle.get((cp.get("name") or "").strip().lower())
        if twin is not None and (cp.get("name") or "").strip():
            have = {v["item_id"] for v in twin["videos"]}
            twin["videos"] += [v for v in vids if v["item_id"] not in have]
            twin["code_identity"] = cp["identity_id"]
        else:
            out.append({**cp, "videos": vids, "code_only": True})
            by_handle.setdefault((cp.get("name") or "").strip().lower(), out[-1])
    return out


def list_for_bc(db: Session, sc, bc_id: str, refresh: bool = False) -> dict:
    """Memory (CACHE_S, per workspace) → the database (ProfilePost; survives restarts) →
    TikTok. A database copy older than CACHE_S is still served at once, marked `stale`, and
    re-read in the background; `refresh` always asks TikTok now."""
    key = f"{getattr(sc, 'user_id', '')}:{bc_id}"
    now = time.time()
    if not refresh:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_S:
            return {**hit[1], "cached": True}
        stored = load_stored(db, bc_id)
        if stored is not None:
            age = now - stored.get("fetched_at", 0)
            if age < CACHE_S:
                _remember(key, stored.get("fetched_at", now), stored)
                return {**stored, "cached": True}
            schedule_refresh(db, bc_id, getattr(sc, "user_id", None))
            return {**stored, "cached": True, "stale": True}
    out = fetch_bc(db, sc, bc_id)
    if out.get("ok"):
        _remember(key, now, out)
        store(db, out)
    return {**out, "cached": False}


def _remember(key: str, at: float, out: dict) -> None:
    with _LOCK:
        if key not in _CACHE and len(_CACHE) >= CACHE_MAX:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (at, out)


# ---------------------------------------------------------------------------
# the database copy (v147): ProfileFetch + ProfilePost
# ---------------------------------------------------------------------------

def _has_store() -> bool:
    return hasattr(models, "ProfilePost") and hasattr(models, "ProfileFetch")


def store(db: Session, out: dict) -> None:
    """Replace this BC's stored posts with a fresh listing. No network inside — the write
    lock is held only for the rows themselves."""
    if not _has_store():
        return
    import json as _json
    from datetime import datetime as _dt
    try:
        bc_id = out["bc_id"]
        db.query(models.ProfilePost).filter(models.ProfilePost.bc_id == bc_id).delete(synchronize_session=False)
        now = _dt.utcnow()
        for p in out.get("profiles") or []:
            for i, v in enumerate(p.get("videos") or []):
                db.add(models.ProfilePost(
                    bc_id=bc_id, identity_id=p["identity_id"], item_id=v["item_id"], pos=i,
                    text=v.get("text") or "", type=v.get("type") or "video", slides=int(v.get("slides") or 0),
                    duration=float(v.get("duration") or 0), created=v.get("created") or "", url=v.get("url") or "",
                    auth_code=v.get("auth_code") or "", cover=v.get("cover") or "", preview=v.get("preview") or "",
                    via=v.get("via") or "", via_identity=(v.get("identity_id") or "") if v.get("via") == "code" else "",
                    seen_at=now))
        meta = [{k: p.get(k, "") for k in ("identity_id", "identity_type", "name", "avatar", "error", "code_only")}
                for p in out.get("profiles") or []]
        row = db.query(models.ProfileFetch).filter(models.ProfileFetch.bc_id == bc_id).first()
        if row is None:
            row = models.ProfileFetch(bc_id=bc_id)
            db.add(row)
        row.via, row.profiles, row.error = out.get("via", ""), _json.dumps(meta), ""
        row.fetched_at = _dt.utcfromtimestamp(out.get("fetched_at") or time.time())
        db.commit()
    except Exception:      # noqa: BLE001 — the live answer is still returned; the copy is a convenience
        db.rollback()
        return
    try:                   # own the covers while TikTok's URLs are alive (they die in ~1 h)
        from . import jobs
        jobs.enqueue(db, "post_thumbs", f"Save post covers · BC {out['bc_id']}", {"bc_id": out["bc_id"]}, quiet=True)
    except Exception:      # noqa: BLE001
        pass


def load_stored(db: Session, bc_id: str) -> dict | None:
    """The stored listing in fetch_bc's shape (covers → our own copy where we have one)."""
    if not _has_store():
        return None
    import json as _json
    row = db.query(models.ProfileFetch).filter(models.ProfileFetch.bc_id == bc_id).first()
    if row is None:
        return None
    try:
        meta = _json.loads(row.profiles or "[]")
    except ValueError:
        meta = []
    posts: dict[str, list] = {}
    for r in (db.query(models.ProfilePost).filter(models.ProfilePost.bc_id == bc_id)
              .order_by(models.ProfilePost.identity_id, models.ProfilePost.pos)):
        posts.setdefault(r.identity_id, []).append(r)
    owned = _owned_covers()
    profiles = []
    for m in meta:
        vids = [{"item_id": r.item_id, "text": r.text or "", "cover": ("/thumbs/pp/%s.jpg" % r.item_id) if r.item_id in owned else (r.cover or ""),
                 "preview": r.preview or "", "slides": int(r.slides or 0), "type": r.type or "video", "auth_code": r.auth_code or "",
                 "url": r.url or "", "created": r.created or "", "duration": r.duration or 0,
                 "via": (getattr(r, "via", "") or "bc"),
                 **({"identity_id": r.via_identity, "identity_type": "AUTH_CODE"} if getattr(r, "via", "") == "code" and getattr(r, "via_identity", "") else {})}
                for r in posts.get(m.get("identity_id", ""), [])]
        profiles.append({"identity_id": m.get("identity_id", ""), "identity_type": m.get("identity_type") or "BC_AUTH_TT",
                         "bc_id": bc_id, "name": m.get("name", ""), "avatar": m.get("avatar", ""), "videos": vids,
                         "error": m.get("error", ""), **({"code_only": True} if m.get("code_only") else {})})
    at = row.fetched_at.replace(tzinfo=__import__("datetime").timezone.utc).timestamp() if row.fetched_at else 0
    return {"ok": True, "bc_id": bc_id, "via": row.via or "", "profiles": profiles,
            "total": sum(len(p["videos"]) for p in profiles), "fetched_at": at, "from_db": True}


def _owned_covers() -> set[str]:
    try:
        from . import thumbs
        return thumbs.owned_keys("pp")
    except Exception:      # noqa: BLE001
        return set()


def schedule_refresh(db: Session, bc_id: str, user_id=None) -> bool:
    """Queue ONE background re-read per BC (a stale copy was just served)."""
    try:
        from . import jobs
        busy = (db.query(models.Job).filter(models.Job.kind == "profile_refresh",
                                            models.Job.status.in_(("queued", "claimed", "running")))
                .all())
        import json as _json
        def _bc(j):
            try:
                return str(_json.loads(j.payload or "{}").get("bc_id") or "")
            except ValueError:
                return ""
        if any(_bc(j) == str(bc_id) for j in busy):
            return False
        jobs.enqueue(db, "profile_refresh", f"Re-read profiles · BC {bc_id}", {"bc_id": bc_id, "user_id": user_id}, quiet=True)
        return True
    except Exception:      # noqa: BLE001
        return False


def cache_covers(db: Session, bc_id: str, limit: int = 400) -> dict:
    """Own the covers of this BC's stored posts (newest listing first). Skips ones we have."""
    if not _has_store():
        return {"saved": 0, "failed": 0}
    from . import thumbs
    import httpx
    have = thumbs.owned_keys("pp")
    todo = [(r.item_id, r.cover) for r in (db.query(models.ProfilePost.item_id, models.ProfilePost.cover)
                                            .filter(models.ProfilePost.bc_id == bc_id).limit(limit * 3))
            if r.item_id not in have and r.cover and thumbs.allowed(r.cover)][:limit]
    saved = failed = 0
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0), follow_redirects=False) as client:
        for item_id, url in todo:
            ok, _ = thumbs.cache("pp", item_id, url, client)
            saved += ok
            failed += (not ok)
    thumbs.forget_owned()
    return {"saved": saved, "failed": failed}


def ensure_spark_rows(db: Session, sc, items: list[dict]) -> list[models.SparkCode]:
    """A SparkCode row per picked post (found by item id in this workspace, else made),
    in the picked order. Rows made here carry no auth code: the post is launched through
    the profile that lists it (resolve_spark step 2), which is exactly what a BC-shared
    profile allows."""
    from .routes.spark_codes import _group_in_view
    out: list[models.SparkCode] = []
    seen: set[str] = set()
    for it in items:
        item_id = str(it.get("item_id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        row = (sc.owned(db.query(models.SparkCode), models.SparkCode)
               .filter(models.SparkCode.tiktok_item_id == item_id).order_by(models.SparkCode.id).first())
        if row is None:
            handle = str(it.get("handle") or "").strip()
            group = _group_in_view(db, sc, handle) if handle else None
            row = models.SparkCode(
                owner_user_id=sc.owner_for_new,
                name=(str(it.get("text") or "")[:80] or f"{handle or 'profile'} · {item_id[-6:]}"),
                code=str(it.get("auth_code") or ""),
                media_type="CAROUSEL" if str(it.get("type") or "").lower() == "carousel" else "VIDEO",
                tiktok_post_url=str(it.get("url") or ""),
                thumbnail_url="",             # TikTok's cover URLs expire in about an hour — a stale one is worse than none
                tiktok_item_id=item_id,
                group_id=group.id if group is not None else None,
                source="",
                check_state="ok",             # the profile lists the post — it's real and usable
            )
            db.add(row)
            db.flush()
        # own its cover now: the picker's URL is fresh, tomorrow it's dead (thumbs.py; the
        # background pass copies it — never on this request)
        cov = str(it.get("cover") or "")
        if cov and not (getattr(row, "thumbnail_url", "") or ""):
            try:
                from . import thumbs
                if thumbs.have("pp", item_id):
                    row.thumbnail_url = thumbs.url_for("pp", item_id)
                elif thumbs.allowed(cov):
                    row.thumbnail_url = cov
            except Exception:      # noqa: BLE001
                pass
        # the profile the post was picked from — the identity the launch runs it under. A post
        # that came through a spark-code identity (v152) launches through its code instead: the
        # BC-profile shortcut in resolve_spark would name the wrong identity type
        if str(it.get("identity_type") or "") == "AUTH_CODE":
            if it.get("auth_code") and not (row.code or ""):
                row.code = str(it["auth_code"])
        elif it.get("identity_id") and not getattr(row, "identity_id", ""):
            row.identity_id = str(it["identity_id"])
            row.identity_bc_id = str(it.get("bc_id") or "")
        out.append(row)
    db.commit()
    return out


def assign(accounts: list, sparks: list, per_spark: int) -> list[tuple[Any, int | None]]:
    """accounts → spark ids, each spark covering `per_spark` accounts in order; accounts
    beyond the picked sparks get None (they fail cleanly, nothing launches without one)."""
    per_spark = max(int(per_spark or 1), 1)
    pairs = []
    for i, acct in enumerate(accounts):
        si = i // per_spark
        pairs.append((acct, sparks[si].id if si < len(sparks) else None))
    return pairs
