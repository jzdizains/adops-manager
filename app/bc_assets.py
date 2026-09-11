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
SNAPSHOT_AT_KEY = "bc_assets_snapshot_at"   # just the timestamp, so the page's poll
                                            # never has to parse the whole snapshot
WATCH_KEY = "bc_assets_watchlist"   # satellite BCs being connected, in order
MAIN_BC_KEY = "main_bc_id"          # set on the page; guessed from the pixels when empty
PIXELS_KEY = "bc_assets_pixel_ids"  # which of the main BC's pixels this flow uses (empty = all)


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


def chosen_pixels(db: Session) -> set[str]:
    """Which of the main BC's pixels the assets flow works with.

    Empty means all of them, which is what every existing install gets. Choosing one
    matters more than it looks: an unchosen pixel is not read during the audit (one
    refused call and one note fewer per pixel) and, more importantly, is never linked
    to an ad account — a Connect run used to push every pixel the BC owned onto every
    account it touched.
    """
    raw = queries.get_setting(db, PIXELS_KEY, "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def set_chosen_pixels(db: Session, ids) -> None:
    clean = sorted({str(i).strip() for i in (ids or []) if str(i).strip()})
    queries.upsert_setting(db, PIXELS_KEY, ",".join(clean))
    db.commit()


def watchlist(db: Session) -> list[dict]:
    """Satellite BCs we are connecting. A brand-new BC is invisible to the API until its
    invitation is accepted, so it lives here from the moment the operator adds its id."""
    raw = queries.get_setting(db, WATCH_KEY, "")
    try:
        rows = json.loads(raw) if raw else []
    except ValueError:
        rows = []
    return [r for r in rows if isinstance(r, dict) and r.get("bc_id")]


def watch_add(db: Session, bc_id: str, label: str = "") -> None:
    rows = watchlist(db)
    bc_id = "".join(ch for ch in str(bc_id) if ch.isdigit())
    if bc_id and not any(r["bc_id"] == bc_id for r in rows):
        rows.append({"bc_id": bc_id, "label": label.strip()[:80], "added": _now_iso()})
        queries.upsert_setting(db, WATCH_KEY, json.dumps(rows[:200]))
        db.commit()


def watch_remove(db: Session, bc_id: str) -> None:
    rows = [r for r in watchlist(db) if r["bc_id"] != str(bc_id)]
    queries.upsert_setting(db, WATCH_KEY, json.dumps(rows))
    db.commit()


def parse_mode(raw: str) -> str | None:
    """`preview` or `send` — nothing else.

    A missing or misspelt mode used to fall back to preview. That is the worst possible
    default: the run does the reading, reports every step, finishes green — and sends
    nothing. There is no safe guess here, so there is no guess.
    """
    m = (raw or "").strip().lower()
    return m if m in ("preview", "send") else None


def stage(db: Session, bc_id: str, snap: dict | None = None) -> dict:
    """Where a satellite BC is in the journey, from the last audit only (no API calls):
      invisible  → the dashboard's token can't see it: invite + accept still to do
      no_admin   → visible, but the token isn't Admin there (invited as Standard?)
      ready      → Admin: the dashboard can share its ad accounts in
      connected  → every ad account it owns that we know of is already in the main BC
    """
    snap = snap if snap is not None else snapshot(db)
    bcs = {b["bc_id"]: b for b in (snap.get("bcs") or [])}
    row = bcs.get(str(bc_id))
    accounts = [a for a in (snap.get("accounts") or []) if a.get("owner_bc") == str(bc_id)]
    shared = [a for a in accounts if a.get("in_main_bc")]
    if not row:
        code, what = "invisible", "not visible to the dashboard yet"
    elif (row.get("role") or "").upper() != "ADMIN":
        code, what = "no_admin", f"visible, but your role there is {row.get('role') or 'unknown'} — Admin is needed"
    elif accounts and len(shared) == len(accounts):
        code, what = "connected", f"all {len(accounts)} known ad account(s) shared into the main BC"
    else:
        code, what = "ready", ("Admin — ready to connect"
                               + (f" · {len(accounts) - len(shared)} known account(s) still to share" if accounts else ""))
    return {"bc_id": str(bc_id), "name": (row or {}).get("name", ""), "role": (row or {}).get("role", ""),
            "code": code, "what": what, "accounts": len(accounts), "shared": len(shared),
            "portal": f"https://business.tiktok.com/manage/overview?org_id={bc_id}"}


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


def _pixel_linked(db: Session, token: str, main: str, p: dict) -> tuple[list[str], str, bool]:
    """Which ad accounts a BC pixel is linked to — (ids, note, could_we_read_it).

    Two sources, because the documented one does not answer on every account:

      1. /bc/pixel/link/get/ with the pixel CODE. Correct per the docs, and TikTok
         resolves the code to the right asset — it simply refuses some setups with
         40002 "You don't have permission to the asset(<id>)", even for a Business
         Center Admin whose token links pixels with /bc/pixel/link/update/ fine. That
         is an asset-permission setting inside the BC, not something the API can fix.
      2. What the pixel sweep already learned from the ACCOUNT side (/pixel/list/ per
         ad account, which has no such restriction), stored in pixel_links.

    Two routes were tried and are now gone, because TikTok answered them plainly:
    /bc/asset/advertiser/assigned/ rejects asset_type PIXEL outright ("correct is
    MANAGED_BUSINESS_ACCOUNT, TT_ACCOUNT"), and passing the asset id as pixel_code
    gets "Invalid value of 'pixel_code': related asset ID is missing". Keeping either
    would burn a call per pixel per audit to be told the same thing again.

    The third return value is the one that matters: a read that FAILED is not an empty
    result, and rendering it as "no pixel" is what sent several rounds of debugging
    after a wiring bug that did not exist.
    """
    code = (p.get("code") or "").strip()
    note = ""
    if code:
        try:
            return [str(x) for x in tiktok_api.bc_pixel_linked_advertisers(token, main, code)], "", True
        except tiktok_api.TikTokError as e:
            note = f"/bc/pixel/link/get/: {e.message} (code {e.code})"
    local = _pixel_linked_locally(db, p)
    if local is not None:
        p["source"] = "accounts sweep"
        return local, note, True
    return [], note or "no pixel code on this asset", False


def _pixel_linked_locally(db: Session, p: dict) -> list[str] | None:
    """The account-side answer, if the pixel sweep has ever run. None means we have
    nothing stored for this pixel and genuinely do not know."""
    try:
        from sqlalchemy import or_
        code, pid = (p.get("code") or "").strip(), (p.get("id") or "").strip()
        clauses = []
        if code:
            clauses.append(models.PixelLink.pixel_code == code)
        if pid:
            clauses.append(models.PixelLink.pixel_id == pid)
        if not clauses:
            return None
        rows = (db.query(models.PixelLink.advertiser_id)
                .filter(or_(*clauses) if len(clauses) > 1 else clauses[0]).all())
        if not rows:
            # nothing stored for THIS pixel — but if the sweep has stored nothing at all,
            # that is "never run", not "linked to no accounts"
            if not db.query(models.PixelLink.id).first():
                return None
            return []
        return sorted({str(r[0]) for r in rows if r[0]})
    except Exception:      # noqa: BLE001 — a missing table on an old DB must not break the audit
        log.debug("local pixel links unavailable", exc_info=True)
        return None


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

    # what the main BC can USE — owned assets plus everything its partners share in.
    # Connect judges its work by exactly this set, so the audit has to as well.
    say("reading the main BC's ad accounts and partner shares")
    in_main, shared_via, notes_main = main_bc_advertisers(token, main, say)
    snap["errors"].extend(notes_main)
    snap["shared_via"] = shared_via

    # ---- 3. what each pixel / profile is already linked to
    use = chosen_pixels(db)
    snap["chosen_pixels"] = sorted(use)
    for p in snap["pixels"]:
        p["use"] = (not use) or p["id"] in use or p.get("code") in use
    for p in snap["pixels"]:
        if should_stop and should_stop():
            snap["errors"].append("stopped")
            return _store(db, snap)
        if not p["use"]:
            p["linked"], p["read_ok"] = [], True    # not used → not read, and not a gap
            continue
        p["linked"], note, ok = _pixel_linked(db, token, main, p)
        p["read_ok"] = ok
        if note:
            p["error"] = note
        if not ok:
            snap["errors"].append(
                f"could not read which ad accounts pixel “{p['name']}” is linked to: {note}. "
                "Run the pixel sync on the Pixels page — it reads this from the ad accounts, "
                "which TikTok does allow.")
        elif p.get("source") == "accounts sweep":
            snap["pixel_source"] = "accounts sweep"
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
    used_pixels = [p for p in snap["pixels"] if p.get("use")]
    pixel_linked: dict[str, list[str]] = {}
    for p in used_pixels:
        for adv in p["linked"]:
            pixel_linked.setdefault(adv, []).append(p["name"])
    n_pixels = len(used_pixels)
    profile_linked: dict[str, list[str]] = {}
    for pr in snap["profiles"]:
        for adv in pr["linked"]:
            profile_linked.setdefault(adv, []).append(pr["handle"] or pr["name"])
    n_profiles = len(snap["profiles"])
    # every pixel read failed → "no pixel" is not something we know, it is something we
    # could not find out. The two must never look the same on the page.
    pixels_unreadable = bool(used_pixels) and not any(p.get("read_ok") for p in used_pixels)
    snap["pixels_unreadable"] = pixels_unreadable
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
            "shared_via": shared_via.get(adv, ""),
            "pixels": sorted(pixel_linked.get(adv, [])),
            "pixels_unknown": pixels_unreadable,
            "pixels_missing": 0 if pixels_unreadable else max(0, n_pixels - len(pixel_linked.get(adv, []))),
            "profiles": have,
            "profiles_missing": max(0, n_profiles - len(have)),
        })
    ready = (0 if pixels_unreadable else
             sum(1 for r in snap["accounts"]
                 if r["in_main_bc"] and not r["pixels_missing"] and not r["profiles_missing"]))
    snap["summary"] = {
        "accounts": len(snap["accounts"]),
        "in_main_bc": sum(1 for r in snap["accounts"] if r["in_main_bc"]),
        "with_pixel": sum(1 for r in snap["accounts"] if r["pixels"] and not r["pixels_missing"]),
        "all_profiles": sum(1 for r in snap["accounts"] if n_profiles and not r["profiles_missing"]),
        "ready": ready,
        "profiles": n_profiles,
        "pixels": n_pixels,
        "pixels_owned": len(snap["pixels"]),
    }
    say(f"{ready} of {len(snap['accounts'])} account(s) fully wired")
    return _store(db, snap)


def _store(db: Session, snap: dict) -> dict:
    try:
        queries.upsert_setting(db, SNAPSHOT_KEY, json.dumps(snap)[:900_000])
        queries.upsert_setting(db, SNAPSHOT_AT_KEY, str(snap.get("at") or ""))
        db.commit()
    except Exception as e:      # noqa: BLE001 — a snapshot that can't be stored must not kill the job
        db.rollback()
        log.warning("could not store the assets snapshot: %s", e)
    return snap


def snapshot_at(db: Session) -> str:
    """When the last audit finished — one short setting, no JSON parsing. The page
    polls this every couple of seconds, so it must stay cheap."""
    at = queries.get_setting(db, SNAPSHOT_AT_KEY, "")
    if at:
        return at
    return (snapshot(db) or {}).get("at", "")      # first run after the upgrade


def snapshot(db: Session) -> dict:
    raw = queries.get_setting(db, SNAPSHOT_KEY, "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# Writes — ONE ad account at a time, preview first.
#   1. the satellite BC shares the ad account with the main BC   /bc/partner/add/
#   2. the main BC links its pixel(s) to it                      /bc/pixel/link/update/
#   3. the main BC links every TikTok profile to it              /bc/asset/advertiser/assign/
# Step 1 must be called with an ADMIN token OF THE OWNING BC ("a Business Center can
# only share assets it owns"); steps 2 and 3 with an Admin token of the main BC.
# Every step reports the exact request and TikTok's verbatim answer, and a preview
# run sends nothing at all.
# ---------------------------------------------------------------------------
WIRE_KEY = "bc_assets_last_wire"


def _admin_tokens(db: Session) -> tuple[dict[str, str], dict[str, dict], list[str]]:
    """({bc_id: admin token}, {bc_id: info}, notes) across every stored token."""
    admin: dict[str, str] = {}
    seen: dict[str, dict] = {}
    notes: list[str] = []
    for token, _accounts in tokens(db):
        try:
            bcs = _bcs_for_token(token)
        except tiktok_api.TikTokError as e:
            notes.append(f"a token could not list Business Centers: {e.message} (code {e.code})")
            continue
        for bid, info in bcs.items():
            seen.setdefault(bid, info)
            if info["role"].upper() == "ADMIN":
                admin.setdefault(bid, token)
    return admin, seen, notes


def _account_steps(report: dict, step, main: str, main_token: str, advertiser_id: str,
                   pixels: list[dict], profiles: list[dict], say=lambda t: None) -> None:
    """Link the pixel(s) and every profile to ONE ad account (skipping what is already there)."""
    for p in pixels:
        if str(advertiser_id) in (p.get("linked") or []):
            report["steps"].append({"step": f"Link pixel {p['name']} → {advertiser_id}", "ok": True,
                                    "detail": "already linked — skipped", "request": {}})
            continue
        code = p.get("code") or p["id"]
        say(f"linking pixel {p['name']} to {advertiser_id}")
        step(f"Link pixel {p['name']} → {advertiser_id}",
             {"endpoint": "/bc/pixel/link/update/", "bc_id": main, "pixel_code": code,
              "advertiser_ids": [str(advertiser_id)], "relation_status": "LINK"},
             lambda c=code: tiktok_api.bc_pixel_link_update(main_token, main, c, [str(advertiser_id)], "LINK"))
    for pr in profiles:
        label = pr.get("handle") or pr.get("name") or pr["id"]
        if str(advertiser_id) in (pr.get("linked") or []):
            report["steps"].append({"step": f"Link profile {label} → {advertiser_id}", "ok": True,
                                    "detail": "already linked — skipped", "request": {}})
            continue
        say(f"linking profile {label} to {advertiser_id}")
        step(f"Link profile {label} → {advertiser_id}",
             {"endpoint": "/bc/asset/advertiser/assign/", "bc_id": main, "asset_type": "TT_ACCOUNT",
              "asset_id": pr["id"], "advertiser_id": str(advertiser_id)},
             lambda a=pr["id"]: tiktok_api.bc_tt_account_link(main_token, main, a, str(advertiser_id)))


def _stepper(report: dict, dry_run: bool):
    """Records one call — or, in a preview, only what would have been sent."""
    def step(name: str, request: dict, run) -> bool:
        if dry_run:
            report["steps"].append({"step": name, "ok": None, "detail": "not sent (preview)", "request": request})
            return True
        try:
            answer = run()
            report["steps"].append({"step": name, "ok": True, "detail": "TikTok accepted it",
                                    "request": request, "answer": str(answer)[:300]})
            return True
        except tiktok_api.TikTokError as e:
            report["steps"].append({"step": name, "ok": False,
                                    "detail": f"{e.message} (code {e.code})", "request": request})
            return False
        except Exception as e:      # noqa: BLE001 — a bug here must not kill the job
            report["steps"].append({"step": name, "ok": False, "detail": f"{type(e).__name__}: {e}",
                                    "request": request})
            return False
    return step


def _asset_ids(items) -> set[str]:
    """Asset ids out of any BC listing — TikTok spells the key differently per endpoint."""
    out: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        info = it.get("asset_info") if isinstance(it.get("asset_info"), dict) else {}
        v = (it.get("asset_id") or it.get("advertiser_id") or it.get("id")
             or info.get("asset_id") or info.get("advertiser_id"))
        if v:
            out.add(str(v))
    return out


def _partner_shared_ids(token: str, bc_id: str, partner_id: str) -> tuple[set[str], str]:
    """Ad account ids that exist between bc_id and partner_id, whichever side shared them.

    TikTok's share_type enum is SHARED / SHARING, and its documentation does not say which
    way round they read. It does not need to: in this setup the main BC shares nothing
    outward, so one of the two lists is empty and reading both is exactly as correct as
    knowing which is which — without the guess. A note is only raised if BOTH refuse.
    """
    ids: set[str] = set()
    errs: list[str] = []
    for st in tiktok_api.SHARE_TYPES:
        try:
            ids |= _asset_ids(tiktok_api.bc_partner_asset_get(token, bc_id, str(partner_id),
                                                              "ADVERTISER", st))
        except tiktok_api.TikTokError as e:
            errs.append(f"{st}: {e.message} (code {e.code})")
    return ids, ("; ".join(errs) if len(errs) == len(tiktok_api.SHARE_TYPES) else "")


def main_bc_advertisers(token: str, main: str,
                        say=lambda t: None) -> tuple[set[str], dict[str, str], list[str]]:
    """Every ad account the MAIN Business Center can actually use, however it got there.

    Two ways an ad account is usable from the main BC, and the audit used to see only the
    first: the BC owns it (/bc/asset/admin/get/), or a partner BC shared it in
    (/bc/partner/asset/get/ — which is exactly what Connect sets up).
    Reading only the owned list meant every account connected through a partnership was
    reported as "not in the main BC" forever, no matter how well the run had worked.

    Returns (ids, {ad account id: partner BC it came from}, notes).
    """
    ids: set[str] = set()
    via: dict[str, str] = {}
    notes: list[str] = []
    try:
        ids |= _asset_ids(tiktok_api.bc_assets_admin(token, main, "ADVERTISER"))
    except tiktok_api.TikTokError as e:
        notes.append(f"could not list the main BC's own ad accounts: {e.message} (code {e.code})")
    try:
        partners = tiktok_api.bc_partner_list(token, main)
    except tiktok_api.TikTokError as e:
        notes.append(f"could not list the main BC's partners: {e.message} (code {e.code})")
        partners = []
    owned = set(ids)
    pids = sorted(_partner_ids(partners))[:60]           # a run must stay bounded
    for i, pid in enumerate(pids, 1):
        say(f"reading partner {i} of {len(pids)}'s shared ad accounts")
        shared, err = _partner_shared_ids(token, main, pid)
        if err:
            notes.append(f"could not read what partner {pid} shares: {err}")
            continue
        for a in shared:
            if a not in owned:                           # an account the main BC owns is not
                via.setdefault(a, pid)                   # "shared in", whichever list it appears on
        ids |= shared
    return ids, via, notes


def _partner_ids(partners: list[dict]) -> set[str]:
    out: set[str] = set()
    for p in partners or []:
        if not isinstance(p, dict):
            continue
        info = p.get("partner_info") if isinstance(p.get("partner_info"), dict) else {}
        v = info.get("bc_id") or p.get("partner_id") or p.get("bc_id")
        if v:
            out.add(str(v))
    return out


def _usable_from_main(main_token: str, main: str, bc_id: str) -> tuple[set[str], str, bool]:
    """Which ad accounts the MAIN Business Center can actually use right now, and a note
    about the partnership if TikTok doesn't list it yet.

    /bc/partner/add/ answers "ok" as soon as TikTok records the request. The partnership
    itself has to be approved on the receiving side, and until it is, the shared ad
    accounts are not assets of the main BC — so nothing can be linked to them. Asking
    TikTok what it now reports is the only way to know which of the two happened.
    """
    ids: set[str] = set()
    notes: list[str] = []
    ok_owned = ok_shared = True
    try:
        ids |= _asset_ids(tiktok_api.bc_assets_admin(main_token, main, "ADVERTISER"))
    except tiktok_api.TikTokError as e:
        ok_owned = False
        notes.append(f"could not re-read the main BC's ad accounts: {e.message} (code {e.code})")
    shared, err = _partner_shared_ids(main_token, main, str(bc_id))
    ids |= shared
    if err:
        ok_shared = False
        notes.append(f"could not read the partner share: {err}")
    # Both reads have to answer before an absence means anything. If either failed, an id
    # missing from this set proves nothing — carry on and let TikTok refuse the link itself,
    # rather than skipping work that would have succeeded.
    return ids, "; ".join(notes), (ok_owned and ok_shared)


def _partnership_note(main_token: str, main: str, bc_id: str) -> str:
    """Empty when the main BC lists that BC as a partner; otherwise what to do about it."""
    try:
        partners = tiktok_api.bc_partner_list(main_token, main)
    except tiktok_api.TikTokError:
        return ""
    if str(bc_id) in _partner_ids(partners):
        return ""
    return ("the main Business Center does not list this one as a partner yet — TikTok needs the "
            "partnership approved from the main BC (Business Center → Partners → Requests) before "
            "any of its ad accounts can be used")


def connect_bc(db: Session, bc_id: str, role: str = "OPERATOR", dry_run: bool = True,
               email: str = "", on_progress=None) -> dict:
    """One Business Center, one button: share every ad account it owns into the main BC,
    then give each of them the pixel and every profile. Needs an Admin token of THIS BC —
    which is what accepting the invite as Admin gives the dashboard."""
    def say(t: str) -> None:
        if on_progress:
            try:
                on_progress(t)
            except Exception:      # noqa: BLE001
                pass

    report: dict = {"at": _now_iso(), "bc_id": str(bc_id), "dry_run": bool(dry_run), "role": role,
                    "kind": "bc", "steps": []}
    main = main_bc_id(db)
    if not main:
        report["error"] = "No main Business Center is set."
        return _store_wire(db, report)
    if str(bc_id) == main:
        report["error"] = "That IS the main Business Center — its own ad accounts need no sharing."
        return _store_wire(db, report)
    if role not in tiktok_api.ADVERTISER_ROLES:
        role = "OPERATOR"

    admin, seen, notes = _admin_tokens(db)
    report["notes"] = notes
    report["bc_name"] = (seen.get(str(bc_id)) or {}).get("name", str(bc_id))
    main_token = admin.get(main)
    sat_token = admin.get(str(bc_id))
    if not main_token:
        report["error"] = f"No stored token is Admin of the main Business Center ({main})."
        return _store_wire(db, report)
    if not sat_token:
        report["error"] = (f"No stored token is Admin of {report['bc_name']} yet — invite "
                           f"{email or 'your email'} there as Admin, accept it, then re-run the audit "
                           "so this dashboard's token picks up the access.")
        return _store_wire(db, report)

    snap = snapshot(db)
    pixels = [p for p in (snap.get("pixels") or []) if p.get("use", True)]
    profiles = snap.get("profiles") or []
    report["pixels_used"] = [p.get("name") for p in pixels]
    if not (pixels or profiles):
        report["error"] = "Run the audit first — the dashboard doesn't know the main BC's pixels or profiles yet."
        return _store_wire(db, report)

    step = _stepper(report, dry_run)
    say("listing the Business Center's ad accounts")
    try:
        owned = [str(a.get("asset_id")) for a in tiktok_api.bc_assets_admin(sat_token, str(bc_id), "ADVERTISER")
                 if a.get("asset_id")]
    except tiktok_api.TikTokError as e:
        report["error"] = f"could not list that BC's ad accounts: {e.message} (code {e.code})"
        return _store_wire(db, report)
    already = {r["advertiser_id"] for r in (snap.get("accounts") or []) if r.get("in_main_bc")}
    todo = [a for a in owned if a not in already]
    report["accounts"] = owned
    if not owned:
        report["error"] = "That Business Center has no ad accounts."
        return _store_wire(db, report)

    # 1. share them in (batched — TikTok takes up to 50 asset ids per call)
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        say(f"sharing {len(batch)} ad account(s) into the main BC")
        step(f"Share {len(batch)} ad account(s) into the main BC",
             {"endpoint": "/bc/partner/add/", "bc_id": str(bc_id), "partner_id": main,
              "asset_type": "ADVERTISER", "asset_ids": batch, "advertiser_role": role},
             lambda b=batch: tiktok_api.bc_partner_add(sat_token, str(bc_id), main, b, role))
    if not todo:
        report["steps"].append({"step": "Share ad accounts into the main BC", "ok": True,
                                "detail": f"all {len(owned)} already shared — skipped", "request": {}})

    # 2. did the share ACTUALLY land? "/bc/partner/add/ returned ok" is not the same thing:
    #    the partnership has to be approved from the main Business Center, and until it is,
    #    those ad accounts are not assets of the main BC and nothing can be linked to them.
    waiting: list[str] = []
    if dry_run:
        report["steps"].append({"step": "Check what arrives in the main Business Center", "ok": None,
                                "detail": "a preview can't tell — TikTok only answers this after the share is sent",
                                "request": {"endpoint": "/bc/partner/asset/get/", "bc_id": main,
                                            "partner_id": str(bc_id), "share_type": "SHARED + SHARING"}})
        linkable = list(owned)
    else:
        say("checking what actually arrived in the main Business Center")
        usable, read_note, reliable = _usable_from_main(main_token, main, str(bc_id))
        linkable = [a for a in owned if reliable is False or a in usable]
        waiting = [a for a in owned if reliable and a not in usable]
        report["arrived"], report["waiting"] = linkable, waiting
        if read_note:
            report.setdefault("notes", []).append(read_note)
        report["steps"].append({
            "step": "Check what arrived in the main Business Center",
            "ok": not waiting,
            "detail": (f"TikTok reports all {len(linkable)} ad account(s) as usable from the main BC"
                       if not waiting else
                       f"{len(linkable)} of {len(owned)} usable — {len(waiting)} still not visible to the main BC"),
            "request": {"endpoint": "/bc/partner/asset/get/", "bc_id": main,
                        "partner_id": str(bc_id), "share_type": "SHARED + SHARING"}})
        if waiting:
            hint = _partnership_note(main_token, main, str(bc_id))
            report["approval_needed"] = True
            report["approval_hint"] = hint or (
                "TikTok recorded the share but the main Business Center can't see these ad accounts yet. "
                "Open the main BC → Partners and approve the request from this Business Center, then press Connect again.")
            report.setdefault("notes", []).append(report["approval_hint"])

    # 3. pixel + profiles — only for the accounts TikTok actually reports as usable
    for adv in owned:
        if adv in waiting:
            report["steps"].append({
                "step": f"Link pixel + profiles → {adv}", "ok": False,
                "detail": "skipped — the main BC can't see this ad account yet (see the note above)",
                "request": {}})
            continue
        _account_steps(report, step, main, main_token, adv, pixels, profiles, say)

    done = [x for x in report["steps"] if x["ok"] is True]
    bad = [x for x in report["steps"] if x["ok"] is False]
    if dry_run:
        report["summary"] = (f"PREVIEW ONLY — nothing was sent to TikTok. "
                             f"{len(report['steps'])} step(s) would run for {len(owned)} ad account(s).")
    elif waiting:
        report["summary"] = (f"Sent — but {len(waiting)} of {len(owned)} ad account(s) are still waiting for the "
                             f"partnership to be approved in the main Business Center, so nothing could be linked to them."
                             + (f" {len(done)} step(s) ok." if done else ""))
    else:
        report["summary"] = (f"Sent · {len(owned)} ad account(s) · {len(done)} step(s) ok"
                             + (f", {len(bad)} failed" if bad else ""))
    if not dry_run:
        say("re-reading what TikTok now reports")
        fresh = scan(db, on_progress=on_progress)   # the page must show reality, not what it was before the run
        # ...and say what TikTok now confirms, rather than what we asked it to do
        by_id = {r["advertiser_id"]: r for r in (fresh.get("accounts") or [])}
        n_profiles = (fresh.get("summary") or {}).get("profiles", 0)
        confirmed, checks = [], []
        for a in linkable:
            r = by_id.get(a)
            if not r:
                checks.append({"advertiser_id": a, "name": a, "known": False})
                continue
            unknown = bool(r.get("pixels_unknown"))
            full = bool(r.get("in_main_bc") and (r.get("pixels") or unknown)
                        and not r.get("profiles_missing"))
            if full:
                confirmed.append(a)
            checks.append({"advertiser_id": a, "name": r.get("name") or a, "known": True, "ok": full,
                           "pixels_unknown": unknown,
                           "in_main_bc": bool(r.get("in_main_bc")),
                           "shared_via": r.get("shared_via", ""),
                           "pixels": list(r.get("pixels") or []),
                           "profiles": len(r.get("profiles") or []),
                           "profiles_total": n_profiles})
        known = [c for c in checks if c["known"]]
        report["confirmed"], report["checks"] = confirmed, checks
        if known:
            report["summary"] += (f" TikTok now confirms {len(confirmed)} of {len(known)} "
                                  f"ad account(s) fully wired.")
            # "0 of 1" with no reason is the same dead end as "it completed" was — say which
            # of the three checks came back empty.
            miss = [c for c in known if not c["ok"]]
            if miss:
                why = []
                if any(not c["in_main_bc"] for c in miss):
                    why.append("the main BC still doesn't list it")
                if any(not c["pixels"] and not c["pixels_unknown"] for c in miss):
                    why.append("no pixel link came back")
                if any(c["profiles"] < c["profiles_total"] for c in miss):
                    why.append("not every profile came back")
                if why:
                    report["summary"] += " Still missing: " + "; ".join(why) + "."
            if any(c.get("pixels_unknown") for c in known):
                report["summary"] += (" TikTok would not say which ad accounts the pixels are linked to "
                                      "— the pixel column is unknown, not empty. See Diagnostics.")
        elif not waiting:
            report["summary"] += (" These ad accounts aren't in the dashboard yet — sync ad accounts "
                                  "to see their pixel and profile status here.")
    return _store_wire(db, report)


def members(db: Session, bc_id: str) -> dict:
    """Who is in a Business Center (including pending invites). Read-only."""
    admin, seen, notes = _admin_tokens(db)
    token = admin.get(str(bc_id)) or queries.any_access_token(db)
    if not token:
        return {"error": "TikTok isn't connected.", "members": []}
    try:
        rows = tiktok_api.bc_member_list(token, str(bc_id))
    except tiktok_api.TikTokError as e:
        return {"error": f"{e.message} (code {e.code})", "members": []}
    return {"members": [{"user_id": str(m.get("user_id") or ""), "email": m.get("user_email") or "",
                         "name": m.get("user_name") or "", "role": m.get("user_role") or "",
                         "status": m.get("relation_status") or ""} for m in rows],
            "bc_name": (seen.get(str(bc_id)) or {}).get("name", str(bc_id)), "notes": notes}


def invite(db: Session, bc_id: str, email: str, role: str = "ADMIN") -> dict:
    """Invite an email into a BC as Admin (or Standard). Only possible where a stored token
    is already Admin of that BC — TikTok has no 'invite myself' and no accept endpoint, so a
    brand-new BC's first invite is sent from that BC's own login."""
    report: dict = {"at": _now_iso(), "bc_id": str(bc_id), "kind": "invite", "dry_run": False,
                    "steps": [], "email": email}
    admin, seen, notes = _admin_tokens(db)
    report["notes"] = notes
    report["bc_name"] = (seen.get(str(bc_id)) or {}).get("name", str(bc_id))
    token = admin.get(str(bc_id))
    if not token:
        report["error"] = (f"No stored token is Admin of {report['bc_name']}, so TikTok won't accept an "
                           "invitation from here — send the first invite from that Business Center's own login.")
        return _store_wire(db, report)
    if "@" not in (email or ""):
        report["error"] = "That doesn't look like an email address."
        return _store_wire(db, report)
    step = _stepper(report, False)
    step(f"Invite {email} as {role.title()}",
         {"endpoint": "/bc/member/invite/", "bc_id": str(bc_id), "emails": [email], "user_role": role},
         lambda: tiktok_api.bc_member_invite(token, str(bc_id), [email], role))
    bad = [s for s in report["steps"] if s["ok"] is False]
    report["summary"] = ("invitation sent — accept it in TikTok, then re-run the audit"
                         if not bad else "TikTok refused the invitation")
    return _store_wire(db, report)


def wire(db: Session, advertiser_id: str, role: str = "OPERATOR", dry_run: bool = True,
         on_progress=None) -> dict:
    """Give one ad account the pixel and every profile. Returns a report; never raises."""
    def say(t: str) -> None:
        if on_progress:
            try:
                on_progress(t)
            except Exception:      # noqa: BLE001
                pass

    report: dict = {"at": _now_iso(), "advertiser_id": str(advertiser_id), "dry_run": bool(dry_run),
                    "role": role, "steps": []}

    step = _stepper(report, dry_run)

    acct = (db.query(models.AdAccount)
            .filter(models.AdAccount.advertiser_id == str(advertiser_id)).first())
    if acct is None:
        report["error"] = f"{advertiser_id} is not an ad account this dashboard manages."
        return _store_wire(db, report)
    report["account_name"] = acct.advertiser_name or acct.advertiser_id
    main = main_bc_id(db)
    if not main:
        report["error"] = "No main Business Center is set."
        return _store_wire(db, report)
    if role not in tiktok_api.ADVERTISER_ROLES:
        role = "OPERATOR"

    admin, seen, notes = _admin_tokens(db)
    report["notes"] = notes
    main_token = admin.get(main)
    if not main_token:
        report["error"] = ("None of the stored tokens is Admin of the main Business Center "
                           f"({main}) — the pixel and profile links need one.")
        return _store_wire(db, report)

    snap = snapshot(db)
    row = next((r for r in (snap.get("accounts") or []) if r["advertiser_id"] == str(advertiser_id)), {})
    pixels = [p for p in (snap.get("pixels") or []) if p.get("use", True)]
    profiles = snap.get("profiles") or []
    report["pixels_used"] = [p.get("name") for p in pixels]
    if not (pixels or profiles):
        report["error"] = "Run the audit first — the dashboard doesn't know this BC's pixels or profiles yet."
        return _store_wire(db, report)

    # ---- 1. share the ad account into the main BC (only when it isn't there yet)
    owner = acct.owner_bc_id or ""
    if row.get("in_main_bc"):
        report["steps"].append({"step": "Share the ad account into the main BC", "ok": True,
                                "detail": "already there — skipped", "request": {}})
    elif not owner:
        report["steps"].append({"step": "Share the ad account into the main BC", "ok": False,
                                "detail": "this account's owning Business Center is unknown — sync accounts first",
                                "request": {}})
    elif owner == main:
        report["steps"].append({"step": "Share the ad account into the main BC", "ok": True,
                                "detail": "the main BC owns it — nothing to share", "request": {}})
    elif owner not in admin:
        report["steps"].append({"step": "Share the ad account into the main BC", "ok": False,
                                "request": {"bc_id": owner, "partner_id": main},
                                "detail": (f"no stored token is Admin of {seen.get(owner, {}).get('name', owner)} — "
                                           "TikTok only lets a BC share what it owns, so that BC has to authorize once")})
    else:
        say("sharing the ad account into the main BC")
        step("Share the ad account into the main BC",
             {"endpoint": "/bc/partner/add/", "bc_id": owner, "partner_id": main,
              "asset_type": "ADVERTISER", "asset_ids": [str(advertiser_id)], "advertiser_role": role},
             lambda: tiktok_api.bc_partner_add(admin[owner], owner, main, [str(advertiser_id)], role))
        # ...and did it land? A partnership the main BC has not approved records fine and
        # shares nothing, so ask TikTok rather than trusting the "ok".
        if not dry_run:
            say("checking whether the main Business Center can see it")
            usable, read_note, reliable = _usable_from_main(main_token, main, owner)
            if read_note:
                report.setdefault("notes", []).append(read_note)
            if reliable and str(advertiser_id) not in usable:
                report["approval_needed"] = True
                report["approval_hint"] = _partnership_note(main_token, main, owner) or (
                    "TikTok recorded the share, but the main Business Center still can't see this ad account. "
                    "Open the main BC → Partners and approve the request, then press Wire again.")
                report["steps"].append({
                    "step": "Check it arrived in the main Business Center", "ok": False,
                    "detail": "not visible to the main BC yet — the pixel and profiles can't be linked until it is",
                    "request": {"endpoint": "/bc/partner/asset/get/", "bc_id": main,
                                "partner_id": owner, "share_type": "SHARED + SHARING"}})
                report.setdefault("notes", []).append(report["approval_hint"])

    # ---- 2 + 3. the pixel(s) and every profile
    if not report.get("approval_needed"):
        _account_steps(report, step, main, main_token, str(advertiser_id), pixels, profiles, say)

    done = [x for x in report["steps"] if x["ok"] is True]
    bad = [x for x in report["steps"] if x["ok"] is False]
    report["summary"] = (f"PREVIEW ONLY — nothing was sent to TikTok. {len(report['steps'])} step(s) would run."
                         if dry_run
                         else f"Sent · {len(done)} step(s) ok" + (f", {len(bad)} failed" if bad else ""))
    if not dry_run:
        say("re-reading what TikTok now reports")
        scan(db, on_progress=on_progress)      # so the row stops offering work that is already done
    return _store_wire(db, report)


def _store_wire(db: Session, report: dict) -> dict:
    try:
        queries.upsert_setting(db, WIRE_KEY, json.dumps(report)[:200_000])
        db.commit()
    except Exception as e:      # noqa: BLE001
        db.rollback()
        log.warning("could not store the wiring report: %s", e)
    return report


def last_wire(db: Session) -> dict:
    raw = queries.get_setting(db, WIRE_KEY, "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}
