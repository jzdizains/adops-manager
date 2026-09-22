"""/partners — give an email Admin access to a Business Center.

The one move most operators actually need: you create a Business Center on TikTok
(you're its Admin), then invite your working email into it as an Admin so that email
sees and manages every ad account in the BC. That's a single TikTok call —
/bc/member/invite/ with user_role ADMIN — done here through bc_assets.invite (which
picks a stored token that's already Admin of that BC). The page then shows the BC's
live member list so you watch the invite go from pending to accepted without a refresh.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import bc_assets, invite_autoaccept, invite_mail, models, queries, tiktok_api
from ..balances import bc_portal_url
from ..database import get_db
from ..templating import render

router = APIRouter()


def _back(ok: str = "", err: str = "", anchor: str = "") -> RedirectResponse:
    q = f"?ok={quote(ok)}" if ok else (f"?err={quote(err)}" if err else "")
    return RedirectResponse("/partners" + q + (f"#{anchor}" if anchor else ""), status_code=303)


def _digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


@router.get("/partners")
def partners_page(request: Request, db: Session = Depends(get_db)):
    token = queries.any_access_token(db)
    roles = {}
    if token:
        try:
            roles = bc_assets._bcs_for_token(token)
        except tiktok_api.TikTokError:
            roles = {}   # /bc/get/ didn't answer — the form still works, TikTok enforces the role
    known = {bc.bc_id: bc for bc in db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()}
    bcs = []
    for bid in sorted(set(known) | set(roles),
                      key=lambda b: (known[b].name if b in known else roles.get(b, {}).get("name", b) or b).lower()):
        r = roles.get(bid, {})
        role = str(r.get("role") or "")
        bcs.append({"bc_id": bid, "name": (known[bid].name if bid in known else r.get("name")) or bid,
                    "user_role": role, "admin": role.upper() == "ADMIN"})
    # Admin BCs first — those are the ones an invite can actually be sent from.
    bcs.sort(key=lambda b: (not b["admin"], b["name"].lower()))
    last_email = queries.get_setting(db, "partners_last_email", "")
    accepts = (db.query(models.InviteAccept).order_by(models.InviteAccept.id.desc()).limit(25).all())
    return render(request, "partners.html", {
        "title": "Partners", "bcs": bcs, "has_token": bool(token),
        "last_email": last_email, "roles_loaded": bool(roles),
        "portal": bc_portal_url, "bc_roles": tiktok_api.BC_USER_ROLES,
        "autoaccept_on": invite_autoaccept.enabled(db), "mail": invite_mail.status(),
        "join_name": queries.get_setting(db, "invite_join_name", "") or "Admin",
        "accepts": [_accept_row(a) for a in accepts],
    })


def _accept_row(a: models.InviteAccept) -> dict:
    return {"id": a.id, "bc_id": a.bc_id, "bc_name": a.bc_name or a.bc_id, "email": a.email,
            "role": a.role, "status": a.status, "detail": a.detail,
            "updated": a.updated_at.isoformat() if a.updated_at else ""}


@router.get("/partners/autoaccept.json")
def autoaccept_feed(db: Session = Depends(get_db)):
    """Live status of the auto-accept rows, so the page updates itself while a Join runs."""
    rows = db.query(models.InviteAccept).order_by(models.InviteAccept.id.desc()).limit(25).all()
    running = any(r.status in (invite_autoaccept.WAITING, invite_autoaccept.ACCEPTING) for r in rows)
    return JSONResponse({"accepts": [_accept_row(r) for r in rows], "running": running,
                         "on": invite_autoaccept.enabled(db)})


@router.post("/partners/autoaccept")
def autoaccept_toggle(on: str = Form(""), db: Session = Depends(get_db)):
    invite_autoaccept.set_enabled(db, str(on).lower() in ("1", "true", "on", "yes"))
    ok = "Auto-accept is on — new invites will be accepted from the mailbox automatically." if invite_autoaccept.enabled(db) \
        else "Auto-accept is off — invites are accepted manually."
    return _back(ok=ok)


@router.post("/partners/mailbox")
def mailbox_save(host: str = Form(""), user: str = Form(""), password: str = Form(""),
                 port: int = Form(993), folder: str = Form("INBOX"), use_ssl: str = Form("on"),
                 join_name: str = Form(""), db: Session = Depends(get_db)):
    """Save the invite mailbox (IMAP) — stored on the data disk, never rendered back."""
    if join_name.strip():
        queries.set_setting(db, "invite_join_name", join_name.strip()[:80])
    res = invite_mail.save_config(host, user, password, port=port, folder=folder,
                                  use_ssl=str(use_ssl).lower() in ("1", "true", "on", "yes"))
    if not res.get("ok"):
        return _back(err=res.get("error", "Couldn't save the mailbox."))
    return _back(ok="Invite mailbox saved. Use “Test connection” to check it, then turn on Auto-accept.")


@router.post("/partners/mailbox/test")
def mailbox_test(db: Session = Depends(get_db)):
    res = invite_mail.test_connection()
    return _back(ok=res.get("detail", "Mailbox connected.")) if res.get("ok") else _back(err=res.get("error", "Couldn't connect."))


@router.get("/partners/members.json")
def partners_members(bc_id: str = "", db: Session = Depends(get_db)):
    """Live member list of a BC (Admins, Standard members, pending invites) so the
    page can show who has access and reflect a just-sent invite as pending."""
    bid = _digits(bc_id)
    if not bid:
        return JSONResponse({"members": [], "error": "no BC"})
    return JSONResponse(bc_assets.members(db, bid))


@router.post("/partners/invite-admin")
def invite_admin(request: Request, bc_id: str = Form(""), email: str = Form(""),
                 role: str = Form("ADMIN"), db: Session = Depends(get_db)):
    """Invite one email into a BC as Admin (or Standard). Admin = full control of every
    ad account in the BC, no per-account assignment needed."""
    if not queries.any_access_token(db):
        return _back(err="TikTok isn't connected — connect first.")
    bid = _digits(bc_id)
    email = (email or "").strip()
    role = role.upper() if role.upper() in tiktok_api.BC_USER_ROLES else "ADMIN"
    if not bid:
        return _back(err="Pick a Business Center.")
    if "@" not in email or " " in email:
        return _back(err="That email doesn't look right.")
    rep = bc_assets.invite(db, bid, email, role)
    queries.set_setting(db, "partners_last_email", email)
    if rep.get("error"):
        return _back(err=rep["error"], anchor=f"bc-{bid}")
    bad = [s for s in rep.get("steps", []) if s.get("ok") is False]
    if bad:
        detail = (bad[0].get("error") or "TikTok refused the invitation")
        return _back(err=f"TikTok refused the invite: {detail}", anchor=f"bc-{bid}")
    bc_name = next((b.name for b in db.query(models.BusinessCenter).filter_by(bc_id=bid)), "") or bid
    if invite_autoaccept.enabled(db) and invite_mail.is_configured():
        join_name = queries.get_setting(db, "invite_join_name", "") or "Admin"
        invite_autoaccept.start(db, bid, bc_name, email, join_name, role)
        return _back(ok=f"Invite sent to {email} as {role.title()} — auto-accept is running: watching the mailbox, "
                        "then it Joins and confirms. Track it below; no action needed.", anchor=f"bc-{bid}")
    tail = "" if not invite_autoaccept.enabled(db) else " (set up the invite mailbox to auto-accept it, too)."
    return _back(ok=f"Invite sent to {email} as {role.title()}. Accept it from that email's TikTok login — "
                    "it shows as Admin below once accepted, with access to every ad account in the BC." + tail,
                 anchor=f"bc-{bid}")
