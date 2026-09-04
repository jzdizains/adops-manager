"""New-partner setup — the part of "add a partner BC, share assets, add my
email" that TikTok's Marketing API actually supports, run as recorded steps.

Verified against the API reference (BC Partners / BC Members / BC Assets):
  POST /bc/partner/add/    bc_id + partner_id; may share ad accounts
                           (asset_type ADVERTISER only — the ONLY type allowed)
  POST /bc/member/invite/  emails + user_role (ADMIN | STANDARD) [+ ad accounts]
  GET  /bc/member/get/     members incl. pending invites (user_id, relation_status)
  POST /bc/asset/assign/   give a member an asset: TT_ACCOUNT needs tt_account_roles
Both POSTs need the token's user to be an ADMIN of the BC they act on
(/bc/get/ reports user_role per BC).

Not available through the API, per TikTok's own docs: sharing a PIXEL or a
TIKTOK ACCOUNT with a *partner* BC (Business Center website → Partners →
Share assets), and assigning a pixel to a member. Those stay a checklist.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models, tiktok_api

log = logging.getLogger("adops.partners")

DONE, ERROR, PENDING, WAITING, SKIPPED = "done", "error", "pending", "waiting", "skipped"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ids(csv: str) -> list[str]:
    return [x.strip() for x in (csv or "").split(",") if x.strip()]


def _err(e: tiktok_api.TikTokError) -> str:
    return f"{e.message} (code {e.code}{', ' + e.path if e.path else ''})"[:500]


# ---------------------------------------------------------------------------
# what the token can do
# ---------------------------------------------------------------------------

def bc_roles(token: str) -> dict[str, dict]:
    """{bc_id: {name, user_role, status}} from /bc/get/ — user_role tells
    whether the token's user is ADMIN there (required for partner/member calls)."""
    out: dict[str, dict] = {}
    try:
        for item in tiktok_api.list_business_centers(token):
            info = item.get("bc_info") or {}
            bid = str(info.get("bc_id") or item.get("bc_id") or "")
            if bid:
                out[bid] = {"name": info.get("name") or item.get("name") or bid,
                            "user_role": str(item.get("user_role") or ""),
                            "status": str(info.get("status") or "")}
    except tiktok_api.TikTokError as e:
        log.warning("bc/get failed: %s", e)
    return out


def assets(token: str, bc_id: str, asset_type: str) -> list[dict]:
    """[{id, name, extra}] of one asset type in a BC — for the form pickers."""
    rows = []
    for a in tiktok_api.list_bc_assets(token, bc_id, asset_type):
        rows.append({"id": str(a.get("asset_id") or ""), "name": str(a.get("asset_name") or a.get("asset_id") or ""),
                     "extra": str(a.get("advertiser_role") or ",".join(a.get("tt_account_roles") or []) or "")})
    return [r for r in rows if r["id"]]


# ---------------------------------------------------------------------------
# the steps
# ---------------------------------------------------------------------------

def step_partner(db: Session, row: models.PartnerSetup, token: str) -> bool:
    if not row.partner_id:
        row.partner_status, row.partner_error = SKIPPED, "no partner BC given"
        db.commit()
        return True
    try:
        tiktok_api.bc_partner_add(token, row.bc_id, row.partner_id,
                                  advertiser_ids=_ids(row.share_advertiser_ids) or None,
                                  advertiser_role=row.share_advertiser_role or "OPERATOR")
        row.partner_status, row.partner_error = DONE, ""
    except tiktok_api.TikTokError as e:
        row.partner_status, row.partner_error = ERROR, _err(e)
    row.updated_at = _now()
    db.commit()
    return row.partner_status == DONE


def step_invite(db: Session, row: models.PartnerSetup, token: str) -> bool:
    if not row.invite_email or not row.invite_bc_id:
        row.invite_status, row.invite_error = SKIPPED, "no email / BC given"
        db.commit()
        return True
    try:
        tiktok_api.bc_member_invite(token, row.invite_bc_id, [row.invite_email],
                                    user_role=row.invite_role or "STANDARD",
                                    advertiser_ids=_ids(row.invite_advertiser_ids) or None,
                                    advertiser_role=row.invite_advertiser_role or "OPERATOR")
        row.invite_status, row.invite_error = DONE, ""
        if row.tt_account_id and row.assign_status in (PENDING, SKIPPED, ERROR):
            row.assign_status, row.assign_error = WAITING, "waiting for the invite to be accepted"
    except tiktok_api.TikTokError as e:
        row.invite_status, row.invite_error = ERROR, _err(e)
    row.updated_at = _now()
    db.commit()
    return row.invite_status == DONE


def find_member(token: str, bc_id: str, email: str) -> dict | None:
    """The member row for an email in a BC (pending invites included), or None."""
    email_l = (email or "").strip().lower()
    for m in tiktok_api.bc_member_list(token, bc_id, keyword=email_l):
        if str(m.get("user_email") or "").strip().lower() == email_l:
            return m
    return None


def step_assign(db: Session, row: models.PartnerSetup, token: str) -> bool:
    """Assign the TikTok account to the invited member. Needs the member's
    user_id, which only exists once they accepted — until then: waiting."""
    if not row.tt_account_id:
        row.assign_status = SKIPPED
        db.commit()
        return True
    try:
        m = find_member(token, row.invite_bc_id, row.invite_email)
    except tiktok_api.TikTokError as e:
        row.assign_status, row.assign_error = ERROR, _err(e)
        row.updated_at = _now()
        db.commit()
        return False
    if not m:
        row.assign_status, row.assign_error = WAITING, "invite not accepted yet — TikTok lists no member with that email"
        row.member_status = ""
        db.commit()
        return False
    row.member_status = str(m.get("relation_status") or "")
    row.member_user_id = str(m.get("user_id") or "")
    if not row.member_user_id or (row.member_status and "PEND" in row.member_status.upper()):
        row.assign_status, row.assign_error = WAITING, f"invite status {row.member_status or 'pending'} — re-checked every slow sweep"
        db.commit()
        return False
    try:
        tiktok_api.bc_asset_assign(token, row.invite_bc_id, row.member_user_id, "TT_ACCOUNT",
                                   row.tt_account_id, tt_account_roles=_ids(row.tt_account_roles) or ["POST"])
        row.assign_status, row.assign_error = DONE, ""
    except tiktok_api.TikTokError as e:
        row.assign_status, row.assign_error = ERROR, _err(e)
    row.updated_at = _now()
    db.commit()
    return row.assign_status == DONE


def run(db: Session, row: models.PartnerSetup, token: str) -> None:
    """Run every step that is still pending/errored, in order."""
    if row.partner_status in (PENDING, ERROR):
        step_partner(db, row, token)
    if row.invite_status in (PENDING, ERROR):
        step_invite(db, row, token)
    if row.tt_account_id and row.invite_status == DONE and row.assign_status != DONE:
        step_assign(db, row, token)


def poll(db: Session, token: str | None = None) -> int:
    """Slow-sweep hook: retry every assignment still waiting on an accepted
    invite. Returns how many got assigned this pass."""
    from . import queries
    token = token or queries.any_access_token(db)
    if not token:
        return 0
    n = 0
    for row in (db.query(models.PartnerSetup)
                .filter(models.PartnerSetup.assign_status == WAITING).all()):
        try:
            if step_assign(db, row, token):
                n += 1
        except Exception:   # never let one row break the sweep
            log.exception("partner assign poll failed for setup %s", row.id)
    return n


def is_complete(row: models.PartnerSetup) -> bool:
    return (row.partner_status in (DONE, SKIPPED) and row.invite_status in (DONE, SKIPPED)
            and row.assign_status in (DONE, SKIPPED) and row.pixel_shared and row.profile_shared)
