"""TikTok OAuth connect + token refresh + account sync.

Flow (§3): operator clicks Connect → authorizes in TikTok → callback exchanges
the auth_code for access+refresh tokens → we pull every advertiser under the
Business Center and store AdAccount rows. ONE token is shared by all
advertisers under the BC.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import config, models, queries, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/oauth/connect")
def connect(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(tiktok_api.oauth_authorize_url(state), status_code=303)


@router.get("/oauth/callback")
def callback(request: Request, db: Session = Depends(get_db)):
    err = request.query_params.get("error")
    if err:
        return render(request, "oauth_result.html", {"ok": False, "detail": f"TikTok returned: {err}"})
    auth_code = request.query_params.get("auth_code") or request.query_params.get("code", "")
    if not auth_code:
        return render(request, "oauth_result.html", {"ok": False, "detail": "No auth_code in callback."})
    try:
        tokens = tiktok_api.exchange_auth_code(auth_code)
    except tiktok_api.TikTokError as e:
        return render(request, "oauth_result.html", {"ok": False,
                      "detail": f"Token exchange failed (code {e.code}): {e.message}"})
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expires_in = int(tokens.get("expires_in") or tokens.get("access_token_expire_in") or 86400)
    refresh_expires = int(tokens.get("refresh_token_expire_in") or 30 * 86400)

    result = sync_accounts(db, access_token, refresh_token,
                           datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                           datetime.now(timezone.utc) + timedelta(seconds=refresh_expires))
    return render(request, "oauth_result.html", {"ok": True, "detail":
                  f"Connected. Synced {result['count']} ad account(s)"
                  + (f" across {result['bc_count']} Business Center(s)" if result.get("bc_count") else "") + "."})


def sync_accounts(db: Session, access_token: str, refresh_token: str = "",
                  token_expires_at: datetime | None = None,
                  refresh_expires_at: datetime | None = None) -> dict:
    """Pull the advertiser list (BC assets preferred, OAuth advertisers as
    fallback) and upsert AdAccount rows. The shared token lands on every row."""
    import time as _time

    advertisers: list[dict] = []   # each: {advertiser_id, advertiser_name, bc_id}
    bc_ids: list[str] = []
    bc_members: dict[str, set[str]] = {}   # bc_id -> account ids (SUCCESSFUL listings only)
    sync_errors: list[str] = []
    fetch_complete = True          # False when ANY listing call failed — then we
    now = datetime.now(timezone.utc)  # must NOT retire missing accounts (§ stale rule)
    try:
        bcs = tiktok_api.list_business_centers(access_token)
    except tiktok_api.TikTokError as e:
        bcs = []
        fetch_complete = False
        sync_errors.append(f"BC list failed (code {e.code})")
    # Loop EVERY Business Center under this login (multi-BC support).
    # Errors are isolated PER BC — one broken BC must not sink the others.
    for bc in bcs:
        info = bc.get("bc_info", bc) or {}
        bc_id = str(info.get("bc_id") or bc.get("bc_id", ""))
        if not bc_id:
            continue
        bc_ids.append(bc_id)
        bc_row = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
        if not bc_row:
            bc_row = models.BusinessCenter(bc_id=bc_id)
            db.add(bc_row)
        bc_row.name = info.get("name", info.get("bc_name", "")) or bc_row.name
        bc_row.status = str(info.get("status", "")) or bc_row.status
        bc_row.last_synced_at = now
        try:
            bal, cur = tiktok_api.parse_bc_balance(
                tiktok_api.get_bc_balance(access_token, bc_id))
            bc_row.balance = bal
            if cur:
                bc_row.currency = cur
        except tiktok_api.TikTokError:
            pass
        try:
            members: set[str] = set()
            page = 1
            while True:
                data = tiktok_api.list_bc_advertisers(access_token, bc_id, page=page)
                batch = data.get("list", [])
                for a in batch:
                    aid = str(a.get("asset_id", a.get("advertiser_id", "")))
                    if aid:
                        members.add(aid)
                        advertisers.append({
                            "advertiser_id": aid,
                            "advertiser_name": a.get("asset_name", a.get("advertiser_name", "")),
                            "bc_id": bc_id})
                total_pages = (data.get("page_info", {}) or {}).get("total_page", 1)
                if page >= int(total_pages or 1):
                    break
                page += 1
            bc_members[bc_id] = members   # authoritative membership for this BC
        except tiktok_api.TikTokError as e:
            fetch_complete = False   # this BC's account list is incomplete
            sync_errors.append(f"BC {bc_id} account list failed "
                               f"(code {e.code}: {str(e.message)[:60]})")
        _time.sleep(0.15)            # gentle throttle between BCs (rate limits)
    db.commit()
    # BCs that vanished from the login's list: mark, don't delete
    if fetch_complete and bcs is not None:
        for bc_row in db.query(models.BusinessCenter).all():
            if bc_row.bc_id not in bc_ids:
                bc_row.status = "ACCESS_LOST"
    if not advertisers:
        try:
            advertisers = [{"advertiser_id": str(a.get("advertiser_id", "")),
                            "advertiser_name": a.get("advertiser_name", ""), "bc_id": ""}
                           for a in tiktok_api.get_authorized_advertisers(access_token)]
        except tiktok_api.TikTokError:
            fetch_complete = False

    # Authoritative ownership FIRST: for every BC whose listing SUCCEEDED,
    # accounts it did not list cannot belong to it. Clears stale
    # mass-assignments left by the old single-BC sync — then the upsert below
    # stamps the fresh, correct owner.
    for row in db.query(models.AdAccount).all():
        if row.owner_bc_id in bc_members and \
                row.advertiser_id not in bc_members[row.owner_bc_id]:
            row.owner_bc_id = ""     # unknown until its real BC lists it

    seen_ids: set[str] = set()
    for adv in advertisers:
        if not adv["advertiser_id"]:
            continue
        seen_ids.add(adv["advertiser_id"])
        row = db.query(models.AdAccount).filter_by(advertiser_id=adv["advertiser_id"]).first()
        if not row:
            row = models.AdAccount(advertiser_id=adv["advertiser_id"])
            db.add(row)
        elif row.status == "ACCESS_LOST":
            row.enabled = True         # access came back — reactivate
            row.status = ""
        row.advertiser_name = adv["advertiser_name"] or row.advertiser_name
        row.access_token = access_token
        row.refresh_token = refresh_token or row.refresh_token
        row.token_expires_at = token_expires_at
        row.refresh_expires_at = refresh_expires_at
        row.owner_bc_id = adv.get("bc_id") or row.owner_bc_id
        row.last_synced_at = now

    # Retire accounts that no longer came back — ONLY when the fetch was
    # complete (a partial/failed fetch must never mass-disable real accounts).
    if fetch_complete and seen_ids:
        for row in db.query(models.AdAccount).all():
            if row.advertiser_id not in seen_ids and row.status != "ACCESS_LOST":
                row.enabled = False
                row.status = "ACCESS_LOST"
    db.commit()
    # persist a human-readable sync report (surfaced in the UI — never silent)
    import json as _json
    queries.set_setting(db, "sync_report", _json.dumps({
        "at": now.isoformat(),
        "bcs": len(bc_ids), "bcs_listed_ok": len(bc_members),
        "accounts": len(seen_ids), "complete": fetch_complete,
        "errors": sync_errors[:10],
    }))
    # enrich with advertiser info (status, currency, timezone) — best effort
    try:
        ids = [a["advertiser_id"] for a in advertisers if a["advertiser_id"]][:100]
        if ids:
            for info in tiktok_api.get_advertiser_info(access_token, ids):
                row = db.query(models.AdAccount).filter_by(
                    advertiser_id=str(info.get("advertiser_id", ""))).first()
                if row:
                    row.status = info.get("status", row.status)
                    row.currency = info.get("currency", row.currency)
                    row.timezone = info.get("timezone", row.timezone)
            db.commit()
    except tiktok_api.TikTokError:
        pass
    queries.set_setting(db, "accounts_synced_at", now.isoformat())
    return {"count": len(advertisers), "bc_count": len(bc_ids),
            "bc_id": bc_ids[0] if bc_ids else ""}


@router.post("/oauth/refresh")
def refresh(db: Session = Depends(get_db)):
    """Refresh the shared token before expiry and restamp every account row."""
    acct = db.query(models.AdAccount).filter(models.AdAccount.refresh_token != "").first()
    if not acct:
        return RedirectResponse("/accounts?err=norefresh", status_code=303)
    try:
        tokens = tiktok_api.refresh_access_token(acct.refresh_token)
    except tiktok_api.TikTokError as e:
        return RedirectResponse(f"/accounts?err={e.code}", status_code=303)
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", acct.refresh_token)
    expires_in = int(tokens.get("expires_in") or 86400)
    exp = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    for row in db.query(models.AdAccount).all():
        row.access_token = access_token
        row.refresh_token = refresh_token
        row.token_expires_at = exp
    db.commit()
    return RedirectResponse("/accounts?ok=refreshed", status_code=303)


@router.post("/oauth/manual")
def manual_connect(request: Request, auth_code: str = Form(""), access_token: str = Form(""),
                   db: Session = Depends(get_db)):
    """Bypass the redirect flow entirely: paste an auth_code (copied from the
    address bar after authorizing on TikTok's portal link) OR a ready
    access_token. The exchange endpoint doesn't verify the redirect URI, so
    this works even when OAUTH_REDIRECT_URI was never set."""
    auth_code = auth_code.strip()
    access_token = access_token.strip()
    refresh_token = ""
    expires_at = None
    refresh_expires_at = None
    if auth_code:
        try:
            tokens = tiktok_api.exchange_auth_code(auth_code)
        except tiktok_api.TikTokError as e:
            return render(request, "oauth_result.html", {"ok": False,
                          "detail": f"Auth code exchange failed (code {e.code}): {e.message} "
                                    "— auth codes are single-use and expire fast; generate a fresh one."})
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        expires_in = int(tokens.get("expires_in") or tokens.get("access_token_expire_in") or 86400)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        refresh_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(tokens.get("refresh_token_expire_in") or 30 * 86400))
    if not access_token:
        return render(request, "oauth_result.html", {"ok": False,
                      "detail": "Paste either an auth code or an access token."})
    try:
        result = sync_accounts(db, access_token, refresh_token, expires_at, refresh_expires_at)
    except tiktok_api.TikTokError as e:
        return render(request, "oauth_result.html", {"ok": False,
                      "detail": f"Token rejected by TikTok (code {e.code}): {e.message}"})
    if result["count"] == 0:
        return render(request, "oauth_result.html", {"ok": False,
                      "detail": "The token was accepted but no advertiser accounts came back — "
                                "check the app was authorized by the account that owns your Business Center."})
    return render(request, "oauth_result.html", {"ok": True, "detail":
                  f"Connected manually. Synced {result['count']} ad account(s)"
                  + (f" across {result['bc_count']} Business Center(s)" if result.get("bc_count") else "") + "."})


@router.post("/oauth/sync-accounts")
def resync(db: Session = Depends(get_db)):
    token = queries.any_access_token(db)
    if not token:
        return RedirectResponse("/accounts?err=notoken", status_code=303)
    acct = db.query(models.AdAccount).filter(models.AdAccount.access_token != "").first()
    sync_accounts(db, token, acct.refresh_token if acct else "",
                  acct.token_expires_at if acct else None,
                  acct.refresh_expires_at if acct else None)
    return RedirectResponse("/accounts?ok=synced", status_code=303)
