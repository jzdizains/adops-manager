"""Activity events + per-object timeline + notes.

`record()` is called wherever a person (or a rule) changes something; the
campaign drawer shows `timeline()` — activity events merged with the launch
log and rule actions for that campaign, newest first."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models


def _who(request) -> str:
    try:
        u = getattr(request.state, "user", None)
        return (u.email if u else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def record(db: Session, kind: str, ref_id: str, action: str, detail: str = "",
           request=None, advertiser_id: str = "", by_email: str | None = None, commit: bool = True) -> models.ActivityEvent:
    ev = models.ActivityEvent(kind=kind, ref_id=str(ref_id), advertiser_id=advertiser_id or "",
                              action=action, detail=(detail or "")[:500],
                              by_email=(by_email if by_email is not None else _who(request)))
    db.add(ev)
    if commit:
        db.commit()
    return ev


def timeline(db: Session, campaign_id: str, limit: int = 40) -> list[dict]:
    """[{at, action, detail, who}] newest first for one campaign."""
    items: list[dict] = []
    for ev in (db.query(models.ActivityEvent)
               .filter(models.ActivityEvent.kind == "campaign", models.ActivityEvent.ref_id == campaign_id)
               .order_by(models.ActivityEvent.at.desc()).limit(limit)):
        items.append({"at": ev.at, "action": ev.action, "detail": ev.detail or "", "who": ev.by_email or ""})
    for lg in (db.query(models.LaunchLog).filter(models.LaunchLog.campaign_id == campaign_id)
               .order_by(models.LaunchLog.created_at.desc()).limit(5)):
        items.append({"at": lg.created_at, "action": "launched" if lg.ok else "launch failed",
                      "detail": (f"preset {lg.template_name}" if lg.template_name else "") + (f" · {lg.error_message[:120]}" if not lg.ok and lg.error_message else ""),
                      "who": "launcher"})
    for ra in (db.query(models.RuleAction).filter(models.RuleAction.campaign_id == campaign_id)
               .order_by(models.RuleAction.created_at.desc()).limit(10)):
        items.append({"at": ra.created_at, "action": f"rule: {ra.action or 'paused'}",
                      "detail": f"{ra.rule}" + (f" · {ra.detail[:120]}" if ra.detail else ""), "who": "automation"})
    def _k(i):
        dt = i["at"] or datetime(1970, 1, 1)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    items.sort(key=_k, reverse=True)
    return items[:limit]


# ---- notes ----------------------------------------------------------------
def get_note(db: Session, kind: str, ref_id: str) -> models.Note | None:
    return db.query(models.Note).filter_by(kind=kind, ref_id=str(ref_id)).first()


def set_note(db: Session, kind: str, ref_id: str, text: str, request=None) -> models.Note:
    n = get_note(db, kind, ref_id)
    text = (text or "").strip()[:4000]
    if n is None:
        n = models.Note(kind=kind, ref_id=str(ref_id))
        db.add(n)
    changed = (n.text or "") != text
    n.text = text
    n.by_email = _who(request)
    n.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if changed and kind == "campaign":
        record(db, "campaign", ref_id, "note", (text[:80] + "…") if len(text) > 80 else (text or "(cleared)"), request=request, commit=False)
    db.commit()
    return n


def notes_for(db: Session, kind: str, ref_ids: list[str]) -> dict[str, str]:
    if not ref_ids:
        return {}
    return {n.ref_id: (n.text or "") for n in db.query(models.Note)
            .filter(models.Note.kind == kind, models.Note.ref_id.in_([str(r) for r in ref_ids])) if n.text}
