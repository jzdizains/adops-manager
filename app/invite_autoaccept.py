"""Auto-accept a TikTok Business-Center invite end to end: the Partners page sends the
invite, then this watches the invite mailbox for the one-time link, drives the browser to
click Join, and confirms membership through the official API. It walks a small InviteAccept
row so the Partners page can show the stage live:

    waiting_email → accepting → joined         (or already_member, or error)

Every step is wrapped and the row always ends in a terminal state with a readable reason,
so a stuck accept never blocks the sweep and the operator always sees what to do next."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import bc_invite_accept, invite_mail, models, partners, queries

log = logging.getLogger("adops.invite_autoaccept")

WAITING, ACCEPTING, JOINED, ALREADY, ERROR = "waiting_email", "accepting", "joined", "already_member", "error"
TERMINAL = {JOINED, ALREADY, ERROR}
MAX_ATTEMPTS = 12          # ~ a dozen slow sweeps: enough for the email to land, then give up cleanly
POLL_CAP = 5               # rows advanced per sweep (one browser at a time anyway)
SETTING = "invite_autoaccept"


def enabled(db: Session) -> bool:
    return str(queries.get_setting(db, SETTING, "") or "").lower() in ("1", "true", "on", "yes")


def set_enabled(db: Session, on: bool) -> None:
    queries.set_setting(db, SETTING, "1" if on else "")


def start(db: Session, bc_id: str, bc_name: str, email: str, join_name: str, role: str = "ADMIN") -> models.InviteAccept:
    """Create the tracking row and queue the first attempt."""
    rec = models.InviteAccept(bc_id=str(bc_id), bc_name=str(bc_name or ""), email=str(email or "").strip(),
                              role=str(role or "ADMIN"), join_name=str(join_name or "").strip() or "Admin",
                              status=WAITING, detail="Invite sent — watching the mailbox for the link…")
    db.add(rec)
    db.commit()
    from . import jobs
    jobs.enqueue(db, "invite_autoaccept", f"Auto-accept: {email} → {bc_name or bc_id}",
                 {"record_id": rec.id}, href="/partners")
    return rec


def _member_active(token: str, bc_id: str, email: str) -> bool:
    try:
        m = partners.find_member(token, bc_id, email)
    except Exception:  # noqa: BLE001 — a read hiccup is just "not confirmed yet"
        return False
    if not m:
        return False
    st = str(m.get("relation_status") or "").upper()
    return bool(m.get("user_id")) and "PEND" not in st and "INVIT" not in st


def _set(db: Session, rec: models.InviteAccept, status: str, detail: str) -> None:
    rec.status = status
    rec.detail = detail[:500]
    rec.updated_at = models.utcnow()
    db.commit()


def run(db: Session, rec: models.InviteAccept, token: str | None = None) -> dict:
    """Advance one row by one step. Safe to call repeatedly (from the job and the sweep)."""
    if rec.status in TERMINAL:
        return {"ok": rec.status != ERROR, "detail": rec.detail, "href": "/partners"}
    token = token or queries.any_access_token(db)
    if not token:
        _set(db, rec, ERROR, "TikTok isn't connected, so membership can't be confirmed.")
        return {"ok": False, "detail": rec.detail, "href": "/partners"}
    rec.attempts = (rec.attempts or 0) + 1
    db.commit()

    # Already in? (a returning account is restored on re-invite with no Join at all.)
    if _member_active(token, rec.bc_id, rec.email):
        _set(db, rec, ALREADY, f"{rec.email} already has access to {rec.bc_name or rec.bc_id}.")
        return {"ok": True, "detail": rec.detail, "href": "/partners"}

    if not invite_mail.is_configured():
        _set(db, rec, ERROR, "No invite mailbox is set up (Settings → Invite mailbox), so the link can't be read. "
                             "Accept the invite from the email by hand.")
        return {"ok": False, "detail": rec.detail, "href": "/partners"}

    # Find the invite link in the mailbox.
    url = ""
    if rec.invite_code:
        url = f"https://business.tiktok.com/?invite_code={rec.invite_code}"
    else:
        res = invite_mail.find_invite_links(bc_name_hint=rec.bc_name, since_minutes=180, limit=5)
        if not res.get("ok"):
            _set(db, rec, ERROR, "Couldn't read the invite mailbox: " + str(res.get("error", "")))
            return {"ok": False, "detail": rec.detail, "href": "/partners"}
        invites = res.get("invites") or []
        hint = (rec.bc_name or "").strip().lower()
        match = None
        if hint:
            match = next((i for i in invites if hint in (i.get("bc_name", "") or "").lower()
                          or hint in (i.get("subject", "") or "").lower()), None)
        if not match and len(invites) == 1:
            match = invites[0]           # unambiguous: the only recent invite
        if not match:
            if rec.attempts >= MAX_ATTEMPTS:
                _set(db, rec, ERROR, "The invite email didn't arrive (or several invites were pending and none "
                                     "clearly matched this BC). Accept it from the email by hand.")
                return {"ok": False, "detail": rec.detail, "href": "/partners"}
            _set(db, rec, WAITING, f"Watching the mailbox for the invite to {rec.bc_name or rec.bc_id}… "
                                   f"(check {rec.attempts})")
            return {"ok": True, "detail": rec.detail, "href": "/partners", "waiting": True}
        rec.invite_code = match["invite_code"]
        url = match["url"]
        db.commit()

    # Drive the browser to Join.
    _set(db, rec, ACCEPTING, "Opening the invite and joining…")
    r = bc_invite_accept.accept_invite(url, rec.join_name,
                                       on_step=lambda t: _set(db, rec, ACCEPTING, t))
    if not r.get("ok"):
        if r.get("retry") and rec.attempts < MAX_ATTEMPTS:
            _set(db, rec, WAITING, "Retrying: " + str(r.get("error", ""))[:200])
            return {"ok": True, "detail": rec.detail, "href": "/partners", "waiting": True}
        _set(db, rec, ERROR, str(r.get("error", "The accept couldn't be completed.")) +
             "  Link: " + url)
        return {"ok": False, "detail": rec.detail, "href": "/partners"}

    # Joined (or was already a member per the browser) — confirm through the API.
    if _member_active(token, rec.bc_id, rec.email):
        _set(db, rec, JOINED, f"{rec.email} joined {rec.bc_name or rec.bc_id} as {rec.role.title()} — "
                              "full access to every ad account in the BC.")
        return {"ok": True, "detail": rec.detail, "href": "/partners"}
    if r.get("action") == "already_member":
        _set(db, rec, ALREADY, r.get("detail", "Access was already in place."))
        return {"ok": True, "detail": rec.detail, "href": "/partners"}
    # Clicked Join but TikTok hasn't flipped the status to active yet — check again next sweep.
    if rec.attempts >= MAX_ATTEMPTS:
        _set(db, rec, ERROR, "Clicked Join, but TikTok hasn't confirmed the membership as active. "
                             "Check the Business Center; you may need to finish a 2-step prompt once.")
        return {"ok": False, "detail": rec.detail, "href": "/partners"}
    _set(db, rec, WAITING, "Clicked Join — waiting for TikTok to confirm the membership…")
    return {"ok": True, "detail": rec.detail, "href": "/partners", "waiting": True}


def poll(db: Session) -> int:
    """Slow-sweep hook: advance rows still waiting/accepting. Bounded; one bad row never
    breaks the sweep. Returns how many reached a terminal state this pass."""
    token = queries.any_access_token(db)
    if not token:
        return 0
    done = 0
    rows = (db.query(models.InviteAccept)
            .filter(models.InviteAccept.status.in_([WAITING, ACCEPTING]))
            .order_by(models.InviteAccept.id.asc()).limit(POLL_CAP).all())
    for rec in rows:
        try:
            run(db, rec, token)
            if rec.status in TERMINAL:
                done += 1
        except Exception:  # noqa: BLE001
            log.exception("invite auto-accept poll failed for row %s", rec.id)
            try:
                _set(db, rec, ERROR, "Unexpected error while auto-accepting — accept from the email by hand.")
            except Exception:  # noqa: BLE001
                pass
    return done
