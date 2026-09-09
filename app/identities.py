"""Creator identities — who an ad can appear as on each ad account.

TikTok's model (Ads Manager → Assets → Identity):
  TT_USER          a TikTok account linked to this ad account (its owner logged in and approved)
  BC_AUTH_TT       a TikTok account the Business Center has access to (BC → Assets → TikTok accounts)
  AUTH_CODE        a creator whose post was authorised on this account with a spark code
  CUSTOMIZED_USER  a custom name + avatar (being phased out by TikTok)

A spark ad only launches when the post's creator is one of the account's identities.
Linking a real TikTok profile (TT_USER / BC_AUTH_TT) can ONLY be done by that
profile's owner approving it in TikTok — no API does it — so this module offers the
two things the dashboard CAN do: show the identities each account has, and authorise
a spark code on an account (which creates the AUTH_CODE identity on the spot).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models, tiktok_api

TYPE_LABELS = {
    "TT_USER": "Linked TikTok account",
    "BC_AUTH_TT": "Business Center creator",
    "AUTH_CODE": "Spark-code creator",
    "CUSTOMIZED_USER": "Custom identity",
}
TYPE_HELP = {
    "TT_USER": "The profile's owner linked it to this ad account in Ads Manager (Assets → Identity).",
    "BC_AUTH_TT": "The Business Center has access to this profile (BC → Assets → TikTok accounts) and this account may use it.",
    "AUTH_CODE": "Created when a spark code from this creator was authorised on this account — usable for that creator's authorised posts.",
    "CUSTOMIZED_USER": "A made-up name and avatar; TikTok is retiring these.",
}


def fetch_account(db: Session, acct: models.AdAccount) -> list[models.IdentityRecord]:
    """Pull the account's identities from TikTok and replace the cached rows."""
    from .routes.campaigns import _account_identities
    found = _account_identities(acct)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.query(models.IdentityRecord).filter_by(advertiser_id=acct.advertiser_id).delete()
    rows = []
    seen: set[tuple[str, str]] = set()
    for i in found:
        iid, itype = str(i.get("identity_id") or ""), str(i.get("identity_type") or "")
        if not iid or (iid, itype) in seen:
            continue
        seen.add((iid, itype))
        row = models.IdentityRecord(
            advertiser_id=acct.advertiser_id, identity_id=iid, identity_type=itype,
            display_name=str(i.get("display_name") or i.get("identity_name") or i.get("username") or "")[:200],
            profile_image=str(i.get("profile_image") or i.get("profile_image_url") or i.get("avatar_icon_web_uri") or "")[:1000],
            bc_id=(acct.owner_bc_id or "") if itype == "BC_AUTH_TT" else "", fetched_at=now)
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def sync(db: Session, accounts: list[models.AdAccount]) -> dict:
    """Refresh identities for these accounts (a job). Returns a report like the pixel sync's."""
    import time as _time
    from . import error_messages
    ok, failures, total = 0, [], 0
    for acct in accounts:
        if not acct.access_token:
            failures.append({"account": acct.advertiser_name or acct.advertiser_id, "advertiser_id": acct.advertiser_id,
                             "code": "no-token", "message": "no access token", "friendly": "Not connected.", "action": "Reconnect TikTok (Ad accounts → Connect)."})
            continue
        try:
            total += len(fetch_account(db, acct))
            ok += 1
            db.commit()
        except tiktok_api.TikTokError as e:
            db.rollback()
            ex = error_messages.explain(e.code, e.message)
            failures.append({"account": acct.advertiser_name or acct.advertiser_id, "advertiser_id": acct.advertiser_id,
                             "code": str(e.code), "message": (e.message or "")[:300], "friendly": ex["friendly"], "action": ex["action"]})
        _time.sleep(0.1)
    return {"at": datetime.now(timezone.utc).isoformat(), "identities": total, "accounts": len(accounts),
            "ok_accounts": ok, "failures": failures[:50]}


def authorize(db: Session, acct: models.AdAccount, code: str) -> dict:
    """Authorise a spark code on ONE account and verify which identity now owns the
    post. Returns {ok, item_id, identity, message, steps[]} — never raises for TikTok
    refusals; the message says what TikTok said and what to do."""
    from .routes.campaigns import _BAD_CODE_RE, _account_identities, _bc_id_for, item_media_type
    steps: list[dict] = []
    code = (code or "").strip()
    if not code:
        return {"ok": False, "message": "Paste the spark code first.", "steps": steps}
    if not acct.access_token:
        return {"ok": False, "message": f"{acct.advertiser_name or acct.advertiser_id} isn't connected — reconnect TikTok first.", "steps": steps}
    # 1) authorise
    item_id = ""
    try:
        data = tiktok_api.authorize_tt_video(acct.access_token, acct.advertiser_id, code)
        d = data if isinstance(data, dict) else {}
        item_id = str(d.get("item_id") or d.get("tiktok_item_id") or "")
        steps.append({"step": "Authorise the code on the account", "ok": True, "detail": "TikTok accepted it" + (f" · post {item_id}" if item_id else "")})
    except tiktok_api.TikTokError as e:
        msg = (e.message or "").strip()
        if _BAD_CODE_RE.search(msg):
            steps.append({"step": "Authorise the code on the account", "ok": False, "detail": f"code {e.code}: {msg[:160]}"})
            return {"ok": False, "steps": steps, "message": f"TikTok rejected the code — “{msg[:120]}”. It's wrong, expired, or the creator switched ad authorization off: "
                                                             "generate a fresh code in the TikTok app (post → ⋯ → Ad settings → Ad authorization) and try again."}
        steps.append({"step": "Authorise the code on the account", "ok": True, "detail": f"already authorised (TikTok said: code {e.code} {msg[:80]})"})
    # 2) which post is it
    if not item_id:
        try:
            info = tiktok_api.tt_video_info(acct.access_token, acct.advertiser_id, auth_code=code)
            d = info if isinstance(info, dict) else {}
            item_id = str(d.get("item_id") or (d.get("item_info") or {}).get("item_id") or "")
        except tiktok_api.TikTokError as e:
            steps.append({"step": "Look the post up", "ok": False, "detail": f"code {e.code}: {(e.message or '')[:120]}"})
    if item_id:
        steps.append({"step": "Look the post up", "ok": True, "detail": f"post id {item_id}"})
    # 3) refresh identities and find the owner
    try:
        rows = fetch_account(db, acct)
        db.commit()
        steps.append({"step": "Refresh the account's identities", "ok": True, "detail": f"{len(rows)} identit{'y' if len(rows) == 1 else 'ies'}: " + ", ".join(f"{TYPE_LABELS.get(r.identity_type, r.identity_type)} {r.display_name or '…' + r.identity_id[-4:]}" for r in rows[:6])})
    except tiktok_api.TikTokError as e:
        db.rollback()
        rows = list(db.query(models.IdentityRecord).filter_by(advertiser_id=acct.advertiser_id))
        steps.append({"step": "Refresh the account's identities", "ok": False, "detail": f"code {e.code}: {(e.message or '')[:120]}"})
    owner = None
    item_type = ""
    if item_id:
        for r in sorted(rows, key=lambda r: {"AUTH_CODE": 0, "TT_USER": 1, "BC_AUTH_TT": 2}.get(r.identity_type, 3)):
            try:
                vinfo = tiktok_api.identity_video_info(acct.access_token, acct.advertiser_id, r.identity_id, r.identity_type, item_id,
                                                       identity_authorized_bc_id=_bc_id_for(acct, r.identity_type))
                owner, item_type = r, item_media_type(vinfo)
                break
            except tiktok_api.TikTokError:
                continue
    if owner is not None:
        steps.append({"step": "Verify who owns the post", "ok": True,
                      "detail": f"{TYPE_LABELS.get(owner.identity_type, owner.identity_type)} “{owner.display_name or owner.identity_id}” can run it" + (f" ({item_type.lower()})" if item_type else "")})
        # remember on the spark code row, if it exists
        sc = db.query(models.SparkCode).filter_by(code=code).first()
        if sc and item_id and not sc.tiktok_item_id:
            sc.tiktok_item_id = item_id
            db.commit()
        return {"ok": True, "item_id": item_id, "item_type": item_type, "steps": steps,
                "identity": {"id": owner.identity_id, "type": owner.identity_type, "name": owner.display_name},
                "message": f"Ready — the post can launch on {acct.advertiser_name or acct.advertiser_id} as “{owner.display_name or owner.identity_id}”."}
    steps.append({"step": "Verify who owns the post", "ok": False,
                  "detail": "none of the account's identities can use this post" if item_id else "post id unknown — TikTok didn't return it"})
    return {"ok": False, "item_id": item_id, "steps": steps,
            "message": "The code was accepted, but no identity on this account can use the post yet. Usually the creator's profile "
                       "needs to be linked: ask them to approve the Business Center's request (BC → Assets → TikTok accounts → Add), "
                       "or link it in Ads Manager → Assets → Identity, then refresh here."}
