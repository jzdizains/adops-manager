"""Per-user workspaces — who sees what.

Every user-made thing carries `owner_user_id`: ad accounts, presets, creatives, display
cards, ad texts, spark codes (and their groups), tags. Everything that hangs off an ad
account — campaigns, spend, postbacks, funnel, audience, launches, notices — is scoped
through the account's owner. Business Centers, locations, the music cache, jobs,
diagnostics and settings stay shared (admin tools / reference data).

Who sees what:
  · a buyer: their own workspace, always — whatever cookies say.
  · the super admin (OWNER_EMAIL): "Everyone" by default (the whole company, exactly
    what the dashboard showed before workspaces), or any one user's workspace, chosen
    with the top-bar switch (POST /view → a cookie). Inside a user's view they see and
    can do exactly what that user sees and does; anything they create there belongs
    to that user.

A Scope is cheap: one indexed query for a buyer (their account ids), none for the
super admin on "Everyone". Page filters are one membership test (`allows`) or one
`WHERE owner_user_id = ?` (`owned`).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import config, models, users

COOKIE = "adops_view"           # "all" | "u:<user id>"
COOKIE_MAX_AGE = 60 * 60 * 24 * 90

# every model that carries an owner — the backfill, the guards and the tests use this
OWNED_MODELS = ("AdAccount", "Template", "Creative", "DisplayCard", "AdText", "SparkCode", "SparkCodeGroup", "Tag", "PageTemplate")


@dataclass
class Scope:
    mode: str                                    # all | user
    ids: set[str] | None                         # advertiser ids in view; None = every account
    user_id: int | None = None                   # whose workspace (None = everyone)
    label: str = ""                              # what the top bar shows
    can_switch: bool = False                     # super admin only
    me_id: int | None = None                     # the logged-in user

    # ---- membership -------------------------------------------------------------------
    def allows(self, advertiser_id: str | None) -> bool:
        """Is this account inside the view? (Everything is, on "Everyone".)"""
        if self.ids is None:
            return True
        return bool(advertiser_id) and str(advertiser_id) in self.ids

    def filter_ids(self, ids) -> list[str]:
        if self.ids is None:
            return list(ids)
        return [i for i in ids if str(i) in self.ids]

    def owns(self, row) -> bool:
        """Does an owned row (preset, creative, spark code…) belong to the view?"""
        if self.user_id is None:
            return True
        return getattr(row, "owner_user_id", None) == self.user_id

    def owned(self, query, model):
        """Restrict a query over an owned model to the view. No-op on "Everyone"."""
        if self.user_id is None:
            return query
        return query.filter(model.owner_user_id == self.user_id)

    @property
    def everything(self) -> bool:
        return self.ids is None

    @property
    def owner_for_new(self) -> int | None:
        """Who a thing created in this view belongs to: the user in view, else me."""
        return self.user_id if self.user_id is not None else self.me_id


def owned_ids(db: Session, user_id: int | None) -> set[str]:
    if user_id is None:
        return set()
    return {r[0] for r in db.query(models.AdAccount.advertiser_id).filter(models.AdAccount.owner_user_id == int(user_id))}


def parse_cookie(raw: str | None) -> int | None:
    """u:<id> → id; anything else → None (Everyone)."""
    v = (raw or "").strip()
    if v.startswith("u:") and v[2:].isdigit():
        return int(v[2:])
    return None


def current(request, db: Session, me: models.User | None) -> Scope:
    if me is None:
        return Scope(mode="user", ids=set(), user_id=-1)          # not logged in: nothing
    if users.is_owner(me):
        uid = parse_cookie(request.cookies.get(COOKIE) if request is not None else "")
        if uid is None:
            return Scope(mode="all", ids=None, label="Everyone", can_switch=True, me_id=me.id)
        u = db.get(models.User, uid)
        if u is None or not u.active:
            return Scope(mode="all", ids=None, label="Everyone", can_switch=True, me_id=me.id)
        return Scope(mode="user", ids=owned_ids(db, u.id), user_id=u.id,
                     label=("Mine" if u.id == me.id else u.email), can_switch=True, me_id=me.id)
    return Scope(mode="user", ids=owned_ids(db, me.id), user_id=me.id, label="Mine", can_switch=False, me_id=me.id)


def for_request(request, db: Session) -> Scope:
    me = getattr(getattr(request, "state", None), "user", None)
    return current(request, db, me)


def switch_options(db: Session, scope: Scope) -> list[dict]:
    """The top-bar choices for the super admin: Everyone, Mine, then every other active user."""
    if not scope.can_switch:
        return []
    opts = [{"value": "all", "label": "Everyone", "on": scope.mode == "all"},
            {"value": f"u:{scope.me_id}", "label": "Mine", "on": scope.mode == "user" and scope.user_id == scope.me_id}]
    for u in db.query(models.User).filter(models.User.active == True).order_by(models.User.email):   # noqa: E712
        if u.id == scope.me_id:
            continue
        opts.append({"value": f"u:{u.id}", "label": u.email, "on": scope.mode == "user" and scope.user_id == u.id})
    return opts


def claim(db: Session, acct: models.AdAccount, user_id) -> bool:
    """An account with no owner yet (connected before workspaces, not yet backfilled)
    goes to the user who first launches to it. Owned accounts never move here."""
    if not user_id or acct.owner_user_id is not None:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if db.get(models.User, uid) is None:
        return False
    acct.owner_user_id = uid
    return True


def super_admin(db: Session) -> models.User | None:
    email = users.norm_email(config.OWNER_EMAIL)
    return db.query(models.User).filter(models.User.email == email).first() if email else None


def backfill(db: Session) -> int:
    """Rows made before workspaces have no owner: they become the super admin's. Runs at
    every start; a no-op once everything is owned. Returns rows touched."""
    owner = super_admin(db)
    if owner is None:
        return 0
    n = 0
    for name in OWNED_MODELS:
        model = getattr(models, name)
        n += (db.query(model).filter(model.owner_user_id.is_(None))
              .update({model.owner_user_id: owner.id}, synchronize_session=False))
    if n:
        db.commit()
    return n


def users_index(db: Session) -> dict[int, models.User]:
    return {u.id: u for u in db.query(models.User).all()}


def event_in_view(event: dict, sc: Scope, sources: set[str] | None = None) -> bool:
    """Live-feed line → does it belong to this view? A line stamped with an advertiser
    id follows the account; a postback line follows its source (the view's sources are
    passed in); a line with neither (system notes) is shown to everyone."""
    if sc.everything:
        return True
    adv = str(event.get("advertiser_id") or "")
    if adv:
        return adv in (sc.ids or set())
    src = str(event.get("source") or "")
    if src:
        return sources is not None and src in sources
    return True


def view_sources(db: Session, sc: Scope) -> set[str] | None:
    """The sources (spark codes / launches) that run on the view's accounts; None = all."""
    if sc.everything:
        return None
    from . import pnl_data
    out = set(pnl_data.campaign_source_map(db, sc.ids).values())
    out |= {sp.source for sp in db.query(models.SparkCode).filter(models.SparkCode.owner_user_id == sc.user_id, models.SparkCode.source != "") if sp.source}
    return out


def view_bc_ids(db: Session, sc: Scope) -> set[str] | None:
    """Business Centers the view's accounts sit in (plus the BCs the user's login
    listed); None = every BC."""
    if sc.everything:
        return None
    out = {r[0] for r in db.query(models.AdAccount.owner_bc_id).filter(models.AdAccount.owner_user_id == sc.user_id) if r[0]}
    out |= {r[0] for r in db.query(models.BusinessCenter.bc_id).filter(models.BusinessCenter.owner_user_id == sc.user_id)}
    return out


def pixels_in_view(db: Session, sc: Scope, rows):
    """PixelRecord rows a view may use: owned by one of its accounts, or shared from
    one of its Business Centers."""
    if sc.everything:
        return list(rows)
    bcs = view_bc_ids(db, sc) or set()
    return [p for p in rows if (p.owner_advertiser_id and sc.allows(p.owner_advertiser_id)) or (p.owner_bc_id and p.owner_bc_id in bcs)]


def from_ctx(db: Session) -> Scope:
    """The view of the request being served (from the auth middleware's context) — for
    code paths that have no Request in hand, like the assistant's tools. Background work
    (no request) sees everything."""
    from . import ctx
    view = ctx.VIEW.get()
    if view is None:
        return Scope(mode="all", ids=None, me_id=ctx.OWNER.get())
    return Scope(mode="user", ids=owned_ids(db, view), user_id=view, me_id=ctx.OWNER.get())
