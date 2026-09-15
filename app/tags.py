"""Campaign tags — operator-made labels with a colour, shown next to the campaign
name on the Campaigns page and picked from a small pop-up.

    Tag          name + colour (one of COLORS), shared by everyone
    CampaignTag  which campaigns carry which tag, keyed by TikTok campaign id

Everything is one small query: the page loads every (campaign_id, tag) pair for
its rows in one go; the pop-up toggles one pair per click. Nothing is cached in
memory, so several operators tagging at once never see each other's stale state.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import models

COLORS = ("green", "red", "amber", "blue", "purple", "pink", "teal", "grey")
NAME_MAX = 32


def clean_name(name: str) -> str:
    return " ".join(str(name or "").split())[:NAME_MAX]


def clean_color(color: str) -> str:
    color = str(color or "").strip().lower()
    return color if color in COLORS else "grey"


def as_dict(t: models.Tag) -> dict:
    return {"id": t.id, "name": t.name, "color": t.color or "grey"}


def _scoped(db: Session, owner_user_id):
    q = db.query(models.Tag)
    return q.filter(models.Tag.owner_user_id == owner_user_id) if owner_user_id is not None else q


def all_tags(db: Session, owner_user_id: int | None = None) -> list[dict]:
    """The view's tags (owner_user_id None = everyone's)."""
    return [as_dict(t) for t in _scoped(db, owner_user_id).order_by(models.Tag.name.asc())]


def create(db: Session, name: str, color: str, owner_user_id: int | None = None) -> tuple[models.Tag | None, str]:
    """(tag, error). A name the owner already has returns their existing tag
    (case-insensitive). Names are unique across the whole dashboard (the table says so),
    so another user's tag with that name is reported, never reused."""
    name = clean_name(name)
    if not name:
        return None, "give the tag a name"
    for t in db.query(models.Tag):
        if t.name.lower() == name.lower():
            if owner_user_id is None or t.owner_user_id == owner_user_id:
                return t, ""
            return None, f"“{t.name}” is taken by another user — pick a different name"
    t = models.Tag(name=name, color=clean_color(color), owner_user_id=owner_user_id)
    db.add(t)
    db.flush()
    return t, ""


def get_in_view(db: Session, tag_id: int, owner_user_id: int | None = None) -> models.Tag | None:
    t = db.get(models.Tag, tag_id)
    if t is None or (owner_user_id is not None and t.owner_user_id != owner_user_id):
        return None
    return t


def update(db: Session, tag_id: int, name: str | None = None, color: str | None = None,
           owner_user_id: int | None = None) -> tuple[models.Tag | None, str]:
    t = get_in_view(db, tag_id, owner_user_id)
    if t is None:
        return None, "that tag no longer exists"
    if name is not None:
        name = clean_name(name)
        if not name:
            return None, "give the tag a name"
        clash = next((o for o in db.query(models.Tag) if o.id != t.id and o.name.lower() == name.lower()), None)
        if clash:
            return None, f"a tag called “{clash.name}” already exists"
        t.name = name
    if color is not None:
        t.color = clean_color(color)
    return t, ""


def delete(db: Session, tag_id: int, owner_user_id: int | None = None) -> int:
    """Removes the tag everywhere. Returns how many campaigns carried it."""
    if get_in_view(db, tag_id, owner_user_id) is None:
        return 0
    n = db.query(models.CampaignTag).filter_by(tag_id=tag_id).delete(synchronize_session=False)
    db.query(models.Tag).filter_by(id=tag_id).delete(synchronize_session=False)
    return int(n or 0)


def set_on(db: Session, campaign_ids: list[str], tag_id: int, on: bool, owner_user_id: int | None = None) -> tuple[int, str]:
    """Put a tag on (or take it off) every given campaign. Returns (changed, error)."""
    if get_in_view(db, tag_id, owner_user_id) is None:
        return 0, "that tag no longer exists"
    ids = list(dict.fromkeys(str(c) for c in campaign_ids if c))[:500]      # de-duplicated, order kept
    if not ids:
        return 0, "no campaign given"
    have = {r.campaign_id for r in db.query(models.CampaignTag)
            .filter(models.CampaignTag.tag_id == tag_id, models.CampaignTag.campaign_id.in_(ids))}
    changed = 0
    if on:
        for cid in ids:
            if cid not in have:
                db.add(models.CampaignTag(campaign_id=cid, tag_id=tag_id))
                changed += 1
    else:
        changed = int(db.query(models.CampaignTag)
                        .filter(models.CampaignTag.tag_id == tag_id, models.CampaignTag.campaign_id.in_(ids))
                        .delete(synchronize_session=False) or 0)
    return changed, ""


def by_campaign(db: Session, campaign_ids=None, owner_user_id: int | None = None) -> dict[str, list[dict]]:
    """campaign_id -> [tag dicts], one query for the whole page (the view's tags only)."""
    tags = {t.id: as_dict(t) for t in _scoped(db, owner_user_id)}
    q = db.query(models.CampaignTag.campaign_id, models.CampaignTag.tag_id)
    if campaign_ids is not None:
        ids = list({str(c) for c in campaign_ids if c})
        if not ids:
            return {}
        q = q.filter(models.CampaignTag.campaign_id.in_(ids))
    out: dict[str, list[dict]] = {}
    for cid, tid in q:
        t = tags.get(tid)
        if t:
            out.setdefault(cid, []).append(t)
    for lst in out.values():
        lst.sort(key=lambda t: t["name"].lower())
    return out
