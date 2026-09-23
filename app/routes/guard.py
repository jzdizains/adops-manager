"""Request guards for per-user workspaces (v116).

`account_in_view` is a FastAPI dependency for every endpoint whose path names an
ad account: the account must be inside the caller's view, or the request is a plain
404 — a buyer pasting another buyer's campaign URL gets nothing, not a hint. It
returns the Scope so the endpoint can keep using it.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import scope as scope_mod
from ..database import get_db


def account_in_view(request: Request, advertiser_id: str, db: Session = Depends(get_db)) -> scope_mod.Scope:
    sc = scope_mod.for_request(request, db)
    if not sc.allows(advertiser_id):
        raise HTTPException(status_code=404, detail="Not in your view")
    return sc


def view(request: Request, db: Session = Depends(get_db)) -> scope_mod.Scope:
    """The caller's view, for endpoints that scope a list rather than one account."""
    return scope_mod.for_request(request, db)


def owned_or_404(sc: scope_mod.Scope, row, what: str = "item"):
    """A row of an owned model (preset, creative, spark code…) must belong to the view."""
    if row is None or not sc.owns(row):
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return row


def creative_in_view(request: Request, creative_id: int, db: Session = Depends(get_db)) -> scope_mod.Scope:
    """Every /creatives/{creative_id}/… endpoint: the creative must be in the view."""
    from .. import models
    sc = scope_mod.for_request(request, db)
    row = db.get(models.Creative, int(creative_id))
    if row is None or not sc.owns(row):
        raise HTTPException(status_code=404, detail="Not in your view")
    return sc


def is_owner(request: Request) -> bool:
    """The super admin (OWNER_EMAIL). Company-wide switches — the shared TikTok web-session
    cookie, the raw error feed across every workspace — are theirs alone."""
    from .. import users as _users
    return _users.is_owner(getattr(getattr(request, "state", None), "user", None))


OWNER_ONLY_MSG = "Only the workspace owner can open that page."
