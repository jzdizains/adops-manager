"""Scaling (v155): which campaigns have earned more, and doing it.

  Scale  — go DEEPER: more ad groups (copies of a proven one) in the same campaign.
  Expand — go WIDER: the same creative launched fresh on accounts that aren't running it.

Both are RECOMMENDED only on lifetime numbers (board_numbers.lifetime), never the date picker's:
on Today a campaign that made $57 at 1.8× over a week reads "$0.29 / —" and would never qualify.
The spend floor is the load-bearing half — ROAS on $2 is one lucky conversion.

Doing it is always the operator's choice, with two optional helpers:
  * "Scale ×N once approved" (ScaleWatch): the copies are made only when TikTok says the source
    ad group DELIVERS — only what passed review is multiplied. A rejection cancels it.
  * Auto-promote (Settings › Scaling, OFF by default): a campaign that clears the bar is scaled
    ONCE by itself, capped by a daily number of new ad groups, every decision logged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

log = logging.getLogger("adops.scaling")

WATCH_TTL_H = 72            # a "once approved" wait gives up after this
DELIVERING = "DELIVERY_OK"


def thresholds(settings: dict) -> dict:
    def f(k, d):
        try:
            return float(settings.get(k) if settings.get(k) not in (None, "") else d)
        except (TypeError, ValueError):
            return d
    return {"min_spend": f("scale_min_spend", 50.0), "min_roas": f("scale_min_roas", 1.5),
            "copies": int(f("scale_copies", 5)) or 5}


def advise(life: dict | None, tab: str, operation_status: str, th: dict) -> list[str]:
    """['scale', 'expand'] when the campaign earned it, [] otherwise. Pure.
    Only campaigns that are ON and delivering (the Active tab) are told to grow."""
    if not life or not life.get("has"):
        return []
    if operation_status != "ENABLE" or tab != "active":
        return []
    if float(life.get("spend") or 0) < th["min_spend"] or float(life.get("roas") or 0) < th["min_roas"]:
        return []
    return ["scale", "expand"]


def why(life: dict, th: dict) -> str:
    return (f"Since launch: ${life['spend']:.0f} spent at {life['roas']:.2f}× ROAS "
            f"(bar: ${th['min_spend']:.0f} and {th['min_roas']:.2f}×, Settings › Scaling)")


def pick_source(db, models, campaign_id: str, delivering_only: bool = True):
    """The ad group to copy: a delivering one, the one that spent most (lifetime); without a
    delivering one (and delivering_only False) the newest one in review. None when none."""
    from sqlalchemy import func
    states = db.query(models.AdgroupState).filter(models.AdgroupState.campaign_id == str(campaign_id)).all()
    if not states:
        return None
    spend = {a: float(s or 0) for a, s in db.query(models.AdgroupSnapshot.adgroup_id, func.sum(models.AdgroupSnapshot.spend))
             .filter(models.AdgroupSnapshot.campaign_id == str(campaign_id)).group_by(models.AdgroupSnapshot.adgroup_id)}
    live = [s for s in states if DELIVERING in (s.secondary_status or "").upper() and (s.operation_status or "ENABLE") == "ENABLE"]
    if live:
        return max(live, key=lambda s: (spend.get(s.adgroup_id, 0.0), s.adgroup_id))
    if delivering_only:
        return None
    from . import health
    waiting = [s for s in states if health.is_pending(s.secondary_status)]
    return max(waiting, key=lambda s: s.status_since or datetime.min) if waiting else None


def queue_copies(db, advertiser_id: str, campaign_id: str, adgroup_id: str, copies: int, why_text: str = "") -> tuple[bool, str]:
    """The existing duplicate job (adgroup_copy): (queued?, message)."""
    from . import adgroup_copy, jobs
    n = max(1, min(int(copies or 1), adgroup_copy.MAX_COPIES))
    job, created = jobs.enqueue_once(db, "adgroup_duplicate", f"Scale ad group ×{n}" + (f" · {why_text}" if why_text else ""),
                                     {"advertiser_id": advertiser_id, "campaign_id": campaign_id, "adgroup_id": adgroup_id, "copies": n},
                                     href="/status")
    if not created:
        return False, f"another duplicate run is {job.status} — try again when it finishes"
    return True, f"Making {n} cop{'y' if n == 1 else 'ies'} of the ad group — the drawer shows them when it's done."


def watch(db, models, owner_user_id, advertiser_id: str, campaign_id: str, adgroup_id: str, copies: int, reason: str = "manual"):
    """Wait for the ad group to deliver, then copy it. One open wait per campaign."""
    open_ = (db.query(models.ScaleWatch).filter(models.ScaleWatch.campaign_id == str(campaign_id),
                                                models.ScaleWatch.status == "waiting").first())
    if open_ is not None:
        open_.copies, open_.adgroup_id = copies, adgroup_id or open_.adgroup_id
        db.commit()
        return open_
    row = models.ScaleWatch(owner_user_id=owner_user_id, advertiser_id=str(advertiser_id), campaign_id=str(campaign_id),
                            adgroup_id=str(adgroup_id or ""), copies=int(copies), reason=reason, status="waiting",
                            detail="waiting for TikTok to approve it")
    db.add(row)
    db.commit()
    return row


def step(state_status: str | None, age_h: float, has_source: bool) -> str:
    """What a waiting watch does now: 'fire' | 'rejected' | 'expire' | 'wait'. Pure."""
    s = (state_status or "").upper()
    if "AUDIT_DENY" in s:
        return "rejected"
    if DELIVERING in s and has_source:
        return "fire"
    if age_h >= WATCH_TTL_H:
        return "expire"
    return "wait"


def tick(db, models) -> int:
    """Background (each sweep): act on waiting watches. Returns how many fired."""
    now = datetime.utcnow()
    fired = 0
    rows = db.query(models.ScaleWatch).filter(models.ScaleWatch.status == "waiting").order_by(models.ScaleWatch.id).limit(50).all()
    for w in rows:
        src = None
        if w.adgroup_id:
            src = db.query(models.AdgroupState).filter(models.AdgroupState.adgroup_id == w.adgroup_id).first()
        if src is None or DELIVERING not in (src.secondary_status or "").upper():
            best = pick_source(db, models, w.campaign_id, delivering_only=True)
            if best is not None:
                src = best
        age_h = (now - (w.created_at or now)).total_seconds() / 3600
        what = step(src.secondary_status if src else "", age_h, src is not None)
        if what == "fire":
            ok, msg = queue_copies(db, w.advertiser_id, w.campaign_id, src.adgroup_id, w.copies,
                                   "auto-promoted winner" if w.reason == "auto" else "approved")
            if ok:
                w.status, w.adgroup_id, w.detail = "queued", src.adgroup_id, "approved — copies queued"
                fired += 1
            else:
                w.detail = msg              # stays waiting; next sweep tries again
        elif what == "rejected":
            w.status, w.detail = "rejected", "TikTok rejected the ad group — nothing was copied"
        elif what == "expire":
            w.status, w.detail = "expired", f"not approved within {WATCH_TTL_H} h — nothing was copied"
        db.commit()
    return fired


# ---- auto-promote (opt-in) ----------------------------------------------------------------------

def promote_plan(candidates: list[dict], already: set, used_today: int, daily_max: int, copies: int) -> list[dict]:
    """Which winners to promote now: best lifetime ROAS first, each campaign ONCE ever, never past
    the day's cap of new ad groups. Pure. candidates: [{campaign_id, roas, spend, …}]"""
    out = []
    room = max(daily_max - used_today, 0)
    for c in sorted(candidates, key=lambda c: (-c["roas"], -c["spend"])):
        if c["campaign_id"] in already:
            continue
        if room < copies:
            break
        out.append(c)
        room -= copies
    return out


def auto_promote(db, models) -> int:
    """Background (hourly): per user with auto-promote ON, scale winners once. Returns watches made."""
    from . import board_numbers, pnl_data, queries, settings_store, timeutil
    made = 0
    for u, us, ids in settings_store.per_user(db):
        if not us.get("autoscale_enabled"):
            continue
        th = thresholds(us)
        copies = max(1, int(us.get("autoscale_copies") or 3))
        daily_max = int(us.get("autoscale_daily_max") or 12)
        tool_cids = {c for (c, a) in db.query(models.LaunchLog.campaign_id, models.LaunchLog.advertiser_id)
                     .filter(models.LaunchLog.ok == True, models.LaunchLog.campaign_id != "")     # noqa: E712
                     if ids is None or a in ids}
        recs = [r for r in db.query(models.CampaignRecord).filter(models.CampaignRecord.operation_status == "ENABLE")
                if r.campaign_id in tool_cids]
        if not recs:
            continue
        life = board_numbers.lifetime(db, models, [r.campaign_id for r in recs], pnl_data.campaign_source_map(db))
        cands = []
        for r in recs:
            lf = life.get(r.campaign_id)
            if lf and lf.get("has") and lf["spend"] >= th["min_spend"] and lf["roas"] >= th["min_roas"]:
                if pick_source(db, models, r.campaign_id) is not None:          # something delivering to copy
                    cands.append({"campaign_id": r.campaign_id, "advertiser_id": r.advertiser_id, "name": r.campaign_name,
                                  "roas": lf["roas"], "spend": lf["spend"]})
        already = {c for (c,) in db.query(models.ScaleWatch.campaign_id).filter(models.ScaleWatch.reason == "auto")}
        midnight = timeutil.local_midnight_utc(0).replace(tzinfo=None)
        used_today = sum(int(c or 0) for (c,) in db.query(models.ScaleWatch.copies)
                         .filter(models.ScaleWatch.reason == "auto", models.ScaleWatch.owner_user_id == u.id,
                                 models.ScaleWatch.created_at >= midnight))
        for c in promote_plan(cands, already, used_today, daily_max, copies):
            src = pick_source(db, models, c["campaign_id"])
            watch(db, models, u.id, c["advertiser_id"], c["campaign_id"], src.adgroup_id if src else "", copies, reason="auto")
            db.add(models.Alert(kind="rule_action", level="info", ref_id=c["advertiser_id"],
                                message=f"Auto-promoted “{c['name']}”: +{copies} ad groups (since launch ${c['spend']:.0f} at {c['roas']:.2f}×). "
                                        "Turn auto-promote off in Settings › Scaling.",
                                href=f"/status?state=all&open={c['campaign_id']}"))
            db.commit()
            made += 1
            log.info("auto-promote: %s ×%s for user %s", c["campaign_id"], copies, u.id)
        try:
            queries.set_setting(db, f"autoscale_last:u{u.id}", json.dumps({"at": datetime.utcnow().isoformat(timespec="seconds"),
                                                                          "candidates": len(cands), "made": made}))
        except Exception:      # noqa: BLE001
            db.rollback()
    return made


def open_watches(db, models, campaign_ids: list[str]) -> dict:
    """{campaign_id: watch} still waiting — for the row chip."""
    out = {}
    ids = [c for c in campaign_ids if c]
    for n in range(0, len(ids), 500):
        for w in db.query(models.ScaleWatch).filter(models.ScaleWatch.campaign_id.in_(ids[n:n + 500]),
                                                    models.ScaleWatch.status == "waiting"):
            out[w.campaign_id] = w
    return out


def prune(db, models, days: int = 60) -> int:
    cut = datetime.utcnow() - timedelta(days=days)
    n = (db.query(models.ScaleWatch).filter(models.ScaleWatch.status != "waiting", models.ScaleWatch.created_at < cut)
         .delete(synchronize_session=False))
    db.commit()
    return int(n or 0)
