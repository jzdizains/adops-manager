"""Warm-up campaigns: launch a small Reach campaign on an ad account, pause it the moment
TikTok approves the ad.

A fresh ad account has no delivery history. A warm-up gives it some at the lowest cost:
one Reach campaign, one post, a small daily budget, launched ACTIVE so it goes through
TikTok's normal review like any other ad. As soon as TikTok approves it and it starts
delivering, the poller below pauses the campaign and records what it spent in the minutes
it ran. Nothing is hidden from review — the ad reviewed is exactly the ad that ran.

Pieces (ported from the reference tool's warm-up, fitted to our engine):
  warmup_fields(...)          the launch recipe — built through launch.synthesize() from a
                              transient preset, so it runs through the ONE launch engine
                              (identity resolution, logging, result page, retry rules)
  own_country_location(...)   "target each account's own country": the account's registered
                              country (/advertiser/info/) → its TikTok location id, cached
  poll(db)                    the background check: approved → paused, rejected → reported
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models, queries, tiktok_api

log = logging.getLogger("adops.warmup")

DEFAULT_BUDGET = 50.0
MIN_BUDGET = 20.0          # TikTok's daily-budget floor for an ad group
WATCH_DAYS = 7             # stop watching a warm-up that never got a verdict
POLL_EVERY_S = 120         # at most one check per 2 minutes (the fast sweep runs every 60 s)
MAX_PER_PASS = 40          # at most this many campaigns checked per pass (one /ad/get/ each)

LIVE = "AD_STATUS_DELIVERY_OK"
IN_REVIEW = {"AD_STATUS_IN_REVIEW", "AD_STATUS_AUDIT", "AD_STATUS_REAUDIT", "AD_STATUS_NOT_DELIVERY"}
STOPPED_TOKENS = ("CAMPAIGN_DISABLE", "ADGROUP_DISABLE", "AD_STATUS_DISABLE", "_DELETE")

STATE_LABELS = {
    "waiting": "Waiting for approval",
    "paused": "Approved · paused",
    "rejected": "Rejected",
    "stopped": "Stopped by hand",
    "expired": "No verdict in 7 days",
}

_last_poll = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clamp_budget(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = DEFAULT_BUDGET
    return round(max(MIN_BUDGET, v), 2)


def warmup_fields(budget=DEFAULT_BUDGET, own_country: bool = True,
                  location_ids: list[str] | None = None) -> dict:
    """The launch recipe for a warm-up. Built through the normal preset synthesis so every
    field the engine expects exists; the preset itself is never saved."""
    from .routes import launch as launch_mod
    settings = {
        "destination_type": "none",            # awareness: no URL, no page, no button, no pixel
        "adgroup_budget": clamp_budget(budget),
        "adgroup_budget_mode": "BUDGET_MODE_DAY",
        "schedule_type": "SCHEDULE_FROM_NOW",
        "location_ids": [] if own_country else [str(x) for x in (location_ids or []) if str(x).strip()],
        "placement_auto": False,               # TikTok placement only
        "comment_disabled": False,
        "video_download_disabled": True,
        "share_disabled": True,
        "creative_source": "spark",
        "ad_text_mode": "fixed",
        "ad_text": " ",                        # a spark ad shows the post's own caption
        "pacing": "PACING_MODE_SMOOTH",
    }
    tmpl = models.Template(id=None, name="Warm-up", objective_type="REACH",
                           campaign_budget_mode="ABO", campaign_budget=None,
                           campaign_name_pattern="warmup_{date}_{time}",
                           adgroup_settings=json.dumps(settings))
    fields = launch_mod.synthesize(tmpl)
    fields["_warmup"] = True
    fields["account_default_geo"] = bool(own_country)
    return fields


def own_country_location(db: Session, acct: models.AdAccount) -> str:
    """The TikTok location id of the country this ad account is registered in ("" if
    TikTok won't say). Cached per account: the country never changes."""
    key = f"acct_country_loc:{acct.advertiser_id}"
    cached = queries.get_setting(db, key, "")
    if cached:
        return cached
    if not acct.access_token:
        return ""
    info = tiktok_api.get_advertiser_info(acct.access_token, [acct.advertiser_id])
    row = info[0] if info else {}
    cc = str(row.get("country") or row.get("registered_area") or "").strip().upper()
    if not cc:
        return ""
    loc = ""
    # the region names we already sync (/tool/region/) usually hold it — no extra call
    for r in db.query(models.RegionName).filter(models.RegionName.region_code != "").all():
        if (r.region_code or "").upper() == cc and str(r.level or "").upper() == "COUNTRY":
            loc = r.region_id
            break
    if not loc:
        for item in tiktok_api.list_regions(acct.access_token, acct.advertiser_id,
                                            placements=["PLACEMENT_TIKTOK"], objective_type="REACH") or []:
            code = str(item.get("region_code") or "").upper()
            level = str(item.get("level") or item.get("area_type") or item.get("region_level") or "").upper()
            if code == cc and level == "COUNTRY":
                loc = str(item.get("location_id") or item.get("region_id") or item.get("id") or "")
                break
    if loc:
        queries.set_setting(db, key, loc)
        db.commit()
    return loc


def _spend_today(acct: models.AdAccount, campaign_id: str) -> float:
    """What the campaign spent today before we caught it — best effort (0 if the report fails)."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = tiktok_api.get_report(acct.access_token, acct.advertiser_id,
                                     dimensions=["campaign_id"], metrics=["spend"],
                                     start_date=day, end_date=day)
        for r in rows:
            if str((r.get("dimensions") or {}).get("campaign_id")) == str(campaign_id):
                return float((r.get("metrics") or {}).get("spend") or 0)
    except (tiktok_api.TikTokError, TypeError, ValueError):
        pass
    return 0.0


def decide(statuses: list[str]) -> str:
    """What the ads' review statuses mean for a waiting warm-up (pure — tested directly):
    'pause' once any ad delivers (and not every ad is still in review), 'rejected' when every
    ad was refused, 'stopped' when someone switched it off by hand, else 'wait'."""
    st = [str(s or "").upper() for s in statuses]
    if not st:
        return "wait"
    if any(s == LIVE for s in st) and not all(s in IN_REVIEW for s in st):
        return "pause"
    if all(("REJECT" in s or "AUDIT_DENY" in s) for s in st):
        return "rejected"
    if all(any(tok in s for tok in STOPPED_TOKENS) for s in st):
        return "stopped"
    return "wait"


def _alert(db: Session, level: str, advertiser_id: str, message: str) -> None:
    db.add(models.Alert(kind="warmup", level=level, ref_id=str(advertiser_id), message=message[:500]))


def poll(db: Session, force: bool = False) -> dict:
    """One pass over the waiting warm-ups. Never raises: one bad account never stops the rest."""
    global _last_poll
    if not force and time.time() - _last_poll < POLL_EVERY_S:
        return {"checked": 0, "paused": 0, "skipped": "throttled"}
    _last_poll = time.time()
    cutoff = _now() - timedelta(days=WATCH_DAYS)
    pending = (db.query(models.LaunchLog)
               .filter(models.LaunchLog.warmup == True,              # noqa: E712
                       models.LaunchLog.ok == True,                  # noqa: E712
                       models.LaunchLog.campaign_id != "",
                       models.LaunchLog.warmup_state == "waiting")
               .order_by(models.LaunchLog.id).limit(MAX_PER_PASS).all())
    checked = paused = 0
    for lg in pending:
        try:
            name = lg.advertiser_name or lg.advertiser_id
            if lg.created_at and lg.created_at < cutoff:
                lg.warmup_state, lg.warmup_done_at = "expired", _now()
                db.commit()
                continue
            acct = db.query(models.AdAccount).filter_by(advertiser_id=lg.advertiser_id).first()
            if not acct or not acct.access_token:
                continue
            data = tiktok_api.list_ads(acct.access_token, acct.advertiser_id, page_size=100,
                                       filtering={"campaign_ids": [lg.campaign_id]}) or {}
            checked += 1
            verdict = decide([a.get("secondary_status") for a in (data.get("list") or [])])
            if verdict == "pause":
                spend = _spend_today(acct, lg.campaign_id)
                tiktok_api.update_campaign_status(acct.access_token, acct.advertiser_id,
                                                  [lg.campaign_id], "DISABLE")
                lg.warmup_state, lg.warmup_done_at, lg.warmup_spend = "paused", _now(), spend
                rec = db.query(models.CampaignRecord).filter_by(campaign_id=lg.campaign_id).first()
                if rec:
                    rec.operation_status = "DISABLE"
                _alert(db, "info", lg.advertiser_id,
                       f"Warm-up on {name} was approved and is now paused"
                       + (f" — it spent ${spend:.2f} before the pause." if spend >= 0.01 else "."))
                paused += 1
            elif verdict == "rejected":
                lg.warmup_state, lg.warmup_done_at = "rejected", _now()
                _alert(db, "warn", lg.advertiser_id,
                       f"Warm-up on {name} was rejected by TikTok's review — open Appeals to see why.")
            elif verdict == "stopped":
                lg.warmup_state, lg.warmup_done_at = "stopped", _now()
            db.commit()
        except tiktok_api.TikTokError as e:
            db.rollback()
            log.warning("warm-up check failed for campaign %s: %s", lg.campaign_id, e)
        except Exception:  # noqa: BLE001 — the sweep must survive one bad row
            db.rollback()
            log.exception("warm-up check crashed for launch %s", lg.id)
    return {"checked": checked, "paused": paused}
