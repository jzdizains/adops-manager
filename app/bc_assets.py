"""Assets audit — which ad accounts actually have the pixel and the TikTok profiles.

The working shape (agreed with the operator, Sep 2026):

    satellite BC  --shares its ad account-->  MAIN BC  --links pixel + every profile-->  ad account

TikTok's API only lets a Business Center share AD ACCOUNTS with a partner
(/bc/partner/add/, asset_type ADVERTISER), so pixels and profiles are never pushed
outward. Instead the ad accounts are shared INTO the main BC, where the pixel is
linked with /bc/pixel/link/update/ and each profile with /bc/asset/advertiser/assign/.

This module only READS. It builds a snapshot of the gap — per ad account: is it in
the main BC, is the pixel linked, which profiles are linked — so the operator can see
the work before anything is written. Every call is wrapped: a BC or asset that can't
be read becomes a note on the row, never an exception.

Reads used (all Admin-level, all GET):
    /bc/get/                            BCs a token can see + user_role
    /bc/asset/admin/get/                every PIXEL / TT_ACCOUNT / ADVERTISER in a BC
    /bc/pixel/link/get/                 ad accounts a pixel is linked to
    /bc/asset/advertiser/assigned/      ad accounts a profile is linked to
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models, queries, tiktok_api

log = logging.getLogger("adops.bc_assets")

SNAPSHOT_KEY = "bc_assets_snapshot"
MAIN_BC_KEY = "main_bc_id"          # set on the page; guessed from the pixels when empty


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def tokens(db: Session) -> list[tuple[str, list[str]]]:
    """Distinct access tokens the dashboard holds → the accounts that use each.
    One token usually covers many ad accounts, so BCs are read once per token."""
    by_token: dict[str, list[str]] = {}
    for a in (db.query(models.AdAccount)
              .filter(models.AdAccount.access_token != "").order_by(models.AdAccount.id).all()):
        by_token.setdefault(a.access_token, []).append(a.advertiser_name or a.advertiser_id)
    return list(by_token.items())


def main_bc_id(db: Session) -> str:
    """The BC that owns the pixel + profiles. Operator's choice, else the BC that owns
    the most pixels the dashboard knows about."""
    chosen = queries.get_setting(db, MAIN_BC_KEY, "").strip()
    if chosen:
        return chosen
    counts: dict[str, int] = {}
    for p in db.query(models.PixelRecord).all():
        if getattr(p, "owner_bc_id", ""):
            counts[p.owner_bc_id] = counts.get(p.owner_bc_id, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _bcs_for_token(token: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        for item in tiktok_api.list_business_centers(token):
            info = item.get("bc_info") or {}
            bid = str(info.get("bc_id") or item.get("bc_id") or "")
            if bid:
                out[bid] = {"bc_id": bid,
                            "name": info.get("name") or item.get("name") or bid,
                            "role": str(item.get("user_role") or ""),
                            "status": str(info.get("status") or "")}
    except tiktok_api.TikTokError as e:
        log.warning("bc/get failed: %s", e)
        raise
    return out


def scan(db: Session, on_progress=None, should_stop=None) -> dict:
    """Read the whole picture and store it as a snapshot. Never raises."""
    def say(t: str) -> None:
        if on_progress:
            try:
                on_progress(t)
            except Exception:      # noqa: BLE001 — progress must never break the scan
                pass

    snap: dict = {"at": _now_iso(), "errors": [], "bcs": [], "pixels": [], "profiles": [],
                  "accounts": [], "main_bc": "", "main_bc_name": "", "admin_on_main": False}

    # ---- 1. which BCs each stored token can see, and where it is Admin
    seen_bcs: dict[str, dict] = {}
    admin_token_for: dict[str, str] = {}       # bc_id -> a token that is ADMIN there
    for token, accounts in tokens(db):
        if should_stop and should_stop():
            snap["errors"].append("stopped")
            return _store(db, snap)
        try:
            bcs = _bcs_for_token(token)
        except tiktok_api.TikTokError as e:
            snap["errors"].append(f"a token could not list Business Centers: {e.message} (code {e.code})")
            continue
        for bid, info in bcs.items():
            row = seen_bcs.setdefault(bid, {**info, "tokens": 0, "accounts": []})
            row["tokens"] += 1
            row["accounts"] = sorted(set(row["accounts"] + accounts))[:6]
            if info["role"].upper() == "ADMIN":
                admin_token_for.setdefault(bid, token)
    say(f"{len(seen_bcs)} Business Center(s) visible")

    main = main_bc_id(db)
    snap["main_bc"] = main
    snap["main_bc_name"] = (seen_bcs.get(main) or {}).get("name", "")
    snap["bcs"] = sorted(seen_bcs.values(), key=lambda r: (r["bc_id"] != main, r["name"].lower()))
    snap["admin_on_main"] = bool(main and main in admin_token_for)
    if not main:
        snap["errors"].append("No main Business Center chosen yet — pick the one that owns the pixel and the profiles.")
        return _store(db, snap)
    token = admin_token_for.get(main) or queries.any_access_token(db)
    if not snap["admin_on_main"]:
        snap["errors"].append("None of the stored tokens is Admin of the main Business Center — "
                              "the asset reads below need an Admin authorization of that BC.")

    # ---- 2. what the main BC owns
    for kind, key in (("PIXEL", "pixels"), ("TT_ACCOUNT", "profiles")):
        try:
            items = tiktok_api.bc_assets_admin(token, main, kind)
        except tiktok_api.TikTokError as e:
            snap["errors"].append(f"could not list {kind.lower()}s of the main BC: {e.message} (code {e.code})")
            items = []
        for it in items:
            snap[key].append({
                "id": str(it.get("asset_id") or ""),
                "code": str(it.get("pixel_code") or it.get("asset_id") or ""),
                "name": it.get("asset_name") or it.get("tt_asset_handle") or str(it.get("asset_id") or ""),
                "handle": it.get("tt_asset_handle") or "",
                "linked": [],
            })
        say(f"main BC: {len(snap['pixels'])} pixel(s), {len(snap['profiles'])} profile(s)")

    try:
        in_main = {str(a.get("asset_id")) for a in tiktok_api.bc_assets_admin(token, main, "ADVERTISER")}
    except tiktok_api.TikTokError as e:
        snap["errors"].append(f"could not list the main BC's ad accounts: {e.message} (code {e.code})")
        in_main = set()

    # ---- 3. what each pixel / profile is already linked to
    for p in snap["pixels"]:
        if should_stop and should_stop():
            snap["errors"].append("stopped")
            return _store(db, snap)
        try:
            data = tiktok_api.bc_pixel_link_get(token, main, p.get("code") or p["id"]) or {}
            p["linked"] = [str(x.get("advertiser_id")) for x in (data.get("list") or []) if x.get("advertiser_id")]
        except tiktok_api.TikTokError as e:
            p["error"] = f"{e.message} (code {e.code})"
    for pr in snap["profiles"]:
        if should_stop and should_stop():
            snap["errors"].append("stopped")
            return _store(db, snap)
        try:
            pr["linked"] = tiktok_api.bc_tt_account_advertisers(token, main, pr["id"])
        except tiktok_api.TikTokError as e:
            pr["error"] = f"{e.message} (code {e.code})"
    say("read the existing links")

    # ---- 4. one row per ad account the dashboard manages
    pixel_linked: dict[str, list[str]] = {}
    for p in snap["pixels"]:
        for adv in p["linked"]:
            pixel_linked.setdefault(adv, []).append(p["name"])
    profile_linked: dict[str, list[str]] = {}
    for pr in snap["profiles"]:
        for adv in pr["linked"]:
            profile_linked.setdefault(adv, []).append(pr["handle"] or pr["name"])
    n_profiles = len(snap["profiles"])
    for a in db.query(models.AdAccount).order_by(models.AdAccount.advertiser_name).all():
        adv = a.advertiser_id
        have = sorted(profile_linked.get(adv, []))
        snap["accounts"].append({
            "advertiser_id": adv,
            "name": a.advertiser_name or adv,
            "enabled": bool(a.enabled),
            "owner_bc": a.owner_bc_id or "",
            "owner_bc_name": (seen_bcs.get(a.owner_bc_id or "") or {}).get("name", ""),
            "in_main_bc": adv in in_main,
            "pixels": sorted(pixel_linked.get(adv, [])),
            "profiles": have,
            "profiles_missing": max(0, n_profiles - len(have)),
        })
    ready = sum(1 for r in snap["accounts"] if r["in_main_bc"] and r["pixels"] and not r["profiles_missing"])
    snap["summary"] = {
        "accounts": len(snap["accounts"]),
        "in_main_bc": sum(1 for r in snap["accounts"] if r["in_main_bc"]),
        "with_pixel": sum(1 for r in snap["accounts"] if r["pixels"]),
        "all_profiles": sum(1 for r in snap["accounts"] if n_profiles and not r["profiles_missing"]),
        "ready": ready,
        "profiles": n_profiles,
        "pixels": len(snap["pixels"]),
    }
    say(f"{ready} of {len(snap['accounts'])} account(s) fully wired")
    return _store(db, snap)


def _store(db: Session, snap: dict) -> dict:
    try:
        queries.upsert_setting(db, SNAPSHOT_KEY, json.dumps(snap)[:900_000])
        db.commit()
    except Exception as e:      # noqa: BLE001 — a snapshot that can't be stored must not kill the job
        db.rollback()
        log.warning("could not store the assets snapshot: %s", e)
    return snap


def snapshot(db: Session) -> dict:
    raw = queries.get_setting(db, SNAPSHOT_KEY, "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}
