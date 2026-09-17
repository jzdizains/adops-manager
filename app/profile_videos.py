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

Dashboard-safe: one BC's listing is a handful of API calls (one per profile page of 50
posts, at most PAGES pages each); the result is cached in memory for CACHE_S seconds and
the cache holds at most CACHE_MAX Business Centers. Nothing here is a credential.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from . import models, tiktok_api

PAGES = 4               # up to 200 posts per profile
PAGE_SIZE = 50
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


def _video(info: dict, handle: str) -> dict:
    item_id = str(info.get("item_id") or "")
    kind = "carousel" if str(info.get("item_type", "")).upper() == "CAROUSEL" else "video"
    return {
        "item_id": item_id,
        "text": (info.get("text") or "")[:200],
        "cover": info.get("video_cover_url") or info.get("poster_url") or info.get("cover_url") or "",
        "type": kind,
        "auth_code": info.get("auth_code") or "",
        "url": info.get("share_url") or (f"https://www.tiktok.com/@{handle}/video/{item_id}" if handle and item_id else ""),
        "created": str(info.get("create_time") or info.get("created_at") or "")[:19],
        "duration": info.get("duration") or 0,
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
        for page in range(1, PAGES + 1):
            try:
                data = tiktok_api.list_tt_videos(acct.access_token, acct.advertiser_id, iid, "BC_AUTH_TT",
                                                 page=page, page_size=PAGE_SIZE, identity_authorized_bc_id=bc_id)
            except tiktok_api.TikTokError as e:
                prof["error"] = f"{e.code}: {e.message[:80]}"
                break
            items = data.get("list", [])
            for item in items:
                v = _video(item.get("item_info", item) if isinstance(item, dict) else {}, handle)
                if v["item_id"] and v["item_id"] not in seen:
                    seen.add(v["item_id"])
                    prof["videos"].append(v)
            if len(items) < PAGE_SIZE:
                break
        profiles.append(prof)
    profiles.sort(key=lambda p: (-len(p["videos"]), p["name"].lower()))
    return {"ok": True, "bc_id": bc_id, "via": acct.advertiser_id, "profiles": profiles,
            "total": sum(len(p["videos"]) for p in profiles), "fetched_at": time.time()}


def list_for_bc(db: Session, sc, bc_id: str, refresh: bool = False) -> dict:
    """Cached per (workspace, BC) for CACHE_S seconds; `refresh` re-reads."""
    key = f"{getattr(sc, 'user_id', '')}:{bc_id}"
    now = time.time()
    if not refresh:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_S:
            return {**hit[1], "cached": True}
    out = fetch_bc(db, sc, bc_id)
    if out.get("ok"):
        with _LOCK:
            if len(_CACHE) >= CACHE_MAX:
                oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
                _CACHE.pop(oldest, None)
            _CACHE[key] = (now, out)
    return {**out, "cached": False}


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
                thumbnail_url=str(it.get("cover") or ""),
                tiktok_item_id=item_id,
                group_id=group.id if group is not None else None,
                source="",
            )
            db.add(row)
            db.flush()
        # the profile the post was picked from — the identity the launch runs it under
        if it.get("identity_id") and not getattr(row, "identity_id", ""):
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
