"""Idle bid bump — "if an ad group hasn't spent anything for an hour, raise its bid
by $0.05" (Settings → Rules → Idle bid bump; each user has their own switch and numbers).

How it knows: the campaign sweep already pulls every hot account's ad groups and their
today-spend once a minute (adgroup_stats). `observe()` keeps one AdgroupBidWatch row per
ad group: today's spend as last seen and the moment it last grew. No extra TikTok calls.

When it acts (`due()` — a pure function, tested):
  * the rule is on and the ad group AND its campaign are enabled (not Smart+ — those
    have no bid to edit);
  * TikTok reports the ad group as delivering (ADGROUP_STATUS_DELIVERY_OK, read live
    from the drawer) — in review, rejected, out of budget, out of balance, not started,
    finished all mean "no spend for a reason a higher bid won't fix";
  * the ad group has a bid to raise (a cost cap or a bid); lowest-cost groups have none;
  * no dayparting schedule (an ad group that is simply outside its hours is not idle);
  * spend has not grown for `bid_bump_idle_min` minutes, counted from the later of the
    last spend, the last bump and the first sighting — so a fresh ad group waits a full
    idle period before its first bump, and every bump starts the clock again;
  * fewer than `bid_bump_max_per_day` bumps today, and the new bid stays under
    `bid_bump_ceiling` when one is set.

The bumps themselves run in a job (fast lane), one TikTok call per ad group, committing
between calls — never inside the sweep's write transaction. Every bump (and refusal) is
a RuleAction row on Health → Automation, and a Diagnostics line.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models

log = logging.getLogger("adops.bid_bump")

DELIVERING = "ADGROUP_STATUS_DELIVERY_OK"
BUMPS_PER_JOB = 60          # one job never runs longer than ~a minute of TikTok calls
KEEP_DAYS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def bid_of(g: dict) -> tuple[str, float]:
    """(field, value) of the bid TikTok holds on an ad group — the cost cap for oCPM
    goals, else the bid for click/CPM billing; ("", 0) when it runs on lowest cost."""
    cap = _f(g.get("conversion_bid_price"))
    if cap > 0:
        return "conversion_bid_price", cap
    bid = _f(g.get("bid_price"))
    if bid > 0:
        return "bid_price", bid
    return "", 0.0


def has_schedule(dayparting: str) -> bool:
    """TikTok's dayparting is a string of 0/1 half-hours; empty or all 1s = all hours."""
    d = (dayparting or "").strip()
    return bool(d) and set(d) != {"1"}


# ---------------------------------------------------------------------------
# observe: called by adgroup_stats.write_account (DB only, inside the sweep's write)
# ---------------------------------------------------------------------------

def observe(db: Session, advertiser_id: str, groups: list[dict], metrics: dict[str, dict], day: str,
            now: datetime | None = None) -> int:
    """Refresh the watch rows for one account from what the sweep just fetched.
    Returns rows touched. Pure DB work — no network."""
    now = now or _now()
    ids = [str(g.get("adgroup_id") or "") for g in groups if g.get("adgroup_id")]
    if not ids:
        return 0
    existing = {w.adgroup_id: w for w in db.query(models.AdgroupBidWatch)
                .filter(models.AdgroupBidWatch.adgroup_id.in_(ids)).all()}
    n = 0
    for g in groups:
        agid = str(g.get("adgroup_id") or "")
        if not agid:
            continue
        spend = _f((metrics.get(agid) or {}).get("spend"))
        w = existing.get(agid)
        if w is None:
            w = models.AdgroupBidWatch(advertiser_id=str(advertiser_id), adgroup_id=agid, day=day,
                                       spend_seen=spend, spend_changed_at=now, bumps_day=day)
            db.add(w)
            existing[agid] = w
        else:
            if w.day != day:
                # a new local day: today's spend starts over — not a change of activity
                w.day = day
                w.spend_seen = spend
                if spend > 0:
                    w.spend_changed_at = now
            elif spend > (w.spend_seen or 0) + 0.001:
                w.spend_changed_at = now
                w.spend_seen = spend
        if w.bumps_day != day:
            w.bumps_day, w.bumps_today = day, 0
        w.campaign_id = str(g.get("campaign_id") or w.campaign_id or "")
        w.adgroup_name = (g.get("adgroup_name") or "")[:200]
        w.operation_status = str(g.get("operation_status") or "")
        w.secondary_status = str(g.get("secondary_status") or "")
        w.dayparting = str(g.get("dayparting") or "")[:400]
        w.bid_field, w.bid_now = bid_of(g)
        w.updated_at = now
        n += 1
    return n


def prune(db: Session) -> int:
    cutoff = _now() - timedelta(days=KEEP_DAYS)
    return db.query(models.AdgroupBidWatch).filter(models.AdgroupBidWatch.updated_at < cutoff).delete(synchronize_session=False)


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------

def due(w, settings: dict, now: datetime, campaign_enabled: bool, smart_plus: bool = False) -> tuple[bool, str]:
    """(bump?, reason). The reason is what Health shows for a skipped ad group."""
    if not settings.get("bid_bump_enabled"):
        return False, "rule off"
    step = _f(settings.get("bid_bump_step"))
    if step <= 0:
        return False, "step is 0"
    if (w.operation_status or "") != "ENABLE":
        return False, "ad group paused"
    if not campaign_enabled:
        return False, "campaign paused"
    if smart_plus:
        return False, "Smart+ campaign — TikTok sets the bid"
    if (w.secondary_status or "") != DELIVERING:
        return False, "not delivering: " + ((w.secondary_status or "unknown status").replace("ADGROUP_STATUS_", "").replace("_", " ").lower())
    if not w.bid_field or (w.bid_now or 0) <= 0:
        return False, "no bid to raise (lowest cost)"
    if has_schedule(w.dayparting):
        return False, "has a dayparting schedule"
    idle_min = max(int(_f(settings.get("bid_bump_idle_min")) or 60), 1)
    since = max(x for x in (w.spend_changed_at, w.last_bump_at) if x is not None)
    idle = (now - since).total_seconds() / 60
    if idle < idle_min:
        return False, f"spent {int(idle)} min ago" if (w.spend_seen or 0) > 0 else f"watched {int(idle)} of {idle_min} min"
    cap = int(_f(settings.get("bid_bump_max_per_day")) or 6)
    if (w.bumps_today or 0) >= cap:
        return False, f"{cap} bumps today already (daily cap)"
    ceiling = _f(settings.get("bid_bump_ceiling"))
    if ceiling > 0 and round((w.bid_now or 0) + step, 2) > ceiling + 1e-9:
        return False, f"at the ${ceiling:.2f} ceiling"
    return True, f"idle {int(idle)} min"


def evaluate(db: Session, settings: dict, ids: set | None, now: datetime | None = None) -> list[int]:
    """Watch-row ids due for a bump for one user (their accounts, their settings).
    Records the skip reason on every other enabled, watched ad group. DB only."""
    if not settings.get("bid_bump_enabled"):
        return []
    if ids is not None and not ids:
        return []
    now = now or _now()
    q = db.query(models.AdgroupBidWatch).filter(models.AdgroupBidWatch.operation_status == "ENABLE",
                                                models.AdgroupBidWatch.updated_at >= now - timedelta(minutes=10))
    if ids is not None:
        q = q.filter(models.AdgroupBidWatch.advertiser_id.in_(list(ids)))
    rows = q.all()
    if not rows:
        return []
    cids = {w.campaign_id for w in rows if w.campaign_id}
    camps = {c.campaign_id: c for c in db.query(models.CampaignRecord)
             .filter(models.CampaignRecord.campaign_id.in_(list(cids) or [""])).all()}
    out = []
    for w in rows:
        c = camps.get(w.campaign_id)
        ok, why = due(w, settings, now, campaign_enabled=bool(c is not None and c.operation_status == "ENABLE"),
                      smart_plus=bool(c is not None and c.is_smart_plus))
        w.skip_reason = "" if ok else why
        if ok:
            out.append(w.id)
    db.commit()
    return out


def schedule(db: Session, settings: dict, ids: set | None, user_id=None) -> int:
    """Sweep entry point for one user: find what's due and hand it to the job lane.
    Returns how many ad groups were queued."""
    from . import jobs
    due_ids = evaluate(db, settings, ids)
    if not due_ids:
        return 0
    if jobs.pending(db, "bid_bump"):
        return 0                    # last round still running — it re-checks each row itself
    step = _f(settings.get("bid_bump_step"))
    jobs.enqueue(db, "bid_bump", f"Idle bid bump +${step:.2f} → {len(due_ids)} ad group{'s' if len(due_ids) != 1 else ''}",
                 {"watch_ids": due_ids[:BUMPS_PER_JOB], "user_id": user_id}, href="/monitor?view=automation", quiet=True)
    return len(due_ids)


# ---------------------------------------------------------------------------
# act (job handler body)
# ---------------------------------------------------------------------------

def run(db: Session, watch_ids: list[int], user_id=None, should_stop=None) -> dict:
    """Raise the bid on each due ad group — one TikTok call each, committed one by one."""
    from . import live_log, settings_store, tiktok_api
    settings = settings_store.get_settings(db, user_id)
    step = _f(settings.get("bid_bump_step"))
    now = _now()
    bumped, skipped, failed = 0, 0, []
    tokens: dict[str, str] = {}
    for wid in watch_ids:
        if should_stop and should_stop():
            break
        w = db.get(models.AdgroupBidWatch, wid)
        if w is None:
            continue
        c = db.query(models.CampaignRecord).filter_by(campaign_id=w.campaign_id).first() if w.campaign_id else None
        ok, why = due(w, settings, now, campaign_enabled=bool(c is not None and c.operation_status == "ENABLE"),
                      smart_plus=bool(c is not None and c.is_smart_plus))
        if not ok:
            w.skip_reason = why
            skipped += 1
            db.commit()
            continue
        if w.advertiser_id not in tokens:
            acct = db.query(models.AdAccount).filter_by(advertiser_id=w.advertiser_id).first()
            tokens[w.advertiser_id] = (acct.access_token if acct else "") or ""
        token = tokens[w.advertiser_id]
        old, new = round(w.bid_now or 0, 2), round((w.bid_now or 0) + step, 2)
        label = "cost cap" if w.bid_field == "conversion_bid_price" else "bid"
        rule = f"idle {int(_f(settings.get('bid_bump_idle_min')) or 60)} min → {label} +${step:.2f}"
        if not token:
            w.skip_reason = "no TikTok token for this account"
            failed.append(f"{w.adgroup_name or w.adgroup_id}: no token")
            db.commit()
            continue
        try:
            tiktok_api.update_adgroup(token, w.advertiser_id, w.adgroup_id, **{w.bid_field: new})
            w.bid_now = new
            w.last_bump_at = now
            w.bumps_today = (w.bumps_today or 0) + 1
            w.skip_reason = ""
            bumped += 1
            db.add(models.RuleAction(advertiser_id=w.advertiser_id, campaign_id=w.campaign_id,
                                     campaign_name=(c.campaign_name if c else ""), rule=rule, metric_value=new,
                                     action="bid", ok=True,
                                     detail=f"{w.adgroup_name or w.adgroup_id}: {label} ${old:.2f} → ${new:.2f} "
                                            f"(no spend since {w.spend_changed_at:%H:%M} UTC; bump {w.bumps_today} of {int(_f(settings.get('bid_bump_max_per_day')) or 6)} today)"))
            live_log.push("info", f"Idle bid bump: {w.adgroup_name or w.adgroup_id} {label} ${old:.2f} → ${new:.2f}",
                          advertiser_id=str(w.advertiser_id))
        except tiktok_api.TikTokError as e:
            msg = f"code {e.code} {(e.message or '')[:120]}"
            w.skip_reason = "TikTok refused: " + msg
            failed.append(f"{w.adgroup_name or w.adgroup_id}: {msg}")
            db.add(models.RuleAction(advertiser_id=w.advertiser_id, campaign_id=w.campaign_id,
                                     campaign_name=(c.campaign_name if c else ""), rule=rule, metric_value=old,
                                     action="bid", ok=False,
                                     detail=f"{w.adgroup_name or w.adgroup_id}: {label} ${old:.2f} → ${new:.2f} refused — {msg}"))
        db.commit()
    return {"bumped": bumped, "skipped": skipped, "failed": failed, "step": step}
