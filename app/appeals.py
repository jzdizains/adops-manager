"""Automatic appeals of TikTok ad-review rejections.

Verified against the Marketing API reference (API Reference → Ad Review):
  GET  /ad/review_info/       reasons + suggestion per rejected ad
  GET  /adgroup/review_info/  ad-group verdict, last_audit_time, appeal_status
  POST /adgroup/appeal/       file the appeal (advertiser_id, adgroup_id,
                              optional ad_id, appeal_reason, attachment_list)
and TikTok's help centre ("One Click Appeal for other ad types"):
  * ONE appeal per rejection; once an ad group or any ad in it has been
    appealed, no further appeal is offered for that ad group
  * a written reason is mandatory in the Ads Manager form
  * TikTok aims to answer within 24 hours

Hence the shape: one Appeal row per (ad group, rejection), the engine never
files twice for the same rejection, and every decision is recorded so the
Appeals page shows exactly what was filed, skipped and answered.

Flow (runs inside the issue scan on the slow sweep):
  sync(db, rejected_ads, scanned_advertisers)
    1. group the scan's rejected ads by ad group; fetch the real reasons
       (/ad/review_info/) and the ad-group verdict (/adgroup/review_info/)
    2. upsert a row per ad group + rejection (dedupe key: last_audit_time)
    3. auto-file when enabled, unless a skip keyword matches the reason or the
       daily cap is reached (then the row waits as pending / skipped)
    4. rows no longer reported rejected by a successfully scanned account are
       marked cleared (nothing to appeal any more)
    5. refresh every appealing row's appeal_status from TikTok
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models, tiktok_api, timeutil
from .settings_store import get_settings

log = logging.getLogger("adops.appeals")

FINAL = ("successful", "failed", "done", "cleared", "dismissed")   # a rejection whose story ended
OPEN = ("pending", "skipped", "appealing", "error")        # still tracking the rejection
APPEALABLE = tiktok_api.AD_REJECTED_STATUSES + tiktok_api.ADGROUP_REJECTED_STATUSES
APPEAL_TEXT_MAX = 512                                       # API model cap for appeal text

TIKTOK_TO_STATUS = {
    "APPEALING": "appealing",
    "APPEAL_SUCCESSFUL": "successful",
    "APPEAL_FAILED": "failed",
    "APPEAL_DONE": "done",
}
STATUS_LABELS = {
    "pending": "Not appealed yet", "skipped": "Skipped (keyword)", "appealing": "Appeal filed — waiting",
    "successful": "Appeal accepted", "done": "Re-reviewed", "failed": "Appeal rejected",
    "error": "Couldn't file", "cleared": "Cleared without appeal", "dismissed": "Dismissed by hand",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_appealable(secondary_status: str) -> bool:
    return str(secondary_status or "") in APPEALABLE


# ---------------------------------------------------------------------------
# reason text
# ---------------------------------------------------------------------------

def render_reason(template: str, *, ad_name: str = "", campaign_name: str = "",
                  reasons: str = "") -> str:
    """Fill {ad_name} {campaign_name} {reasons} without str.format (a stray brace
    in the operator's text must not blow up the sweep); hard-capped at 512."""
    text = (template or "")
    for k, v in (("{ad_name}", ad_name), ("{campaign_name}", campaign_name),
                 ("{reasons}", reasons or "the stated reason")):
        text = text.replace(k, v)
    text = " ".join(text.split())
    return text[:APPEAL_TEXT_MAX]


def skip_keywords(settings: dict) -> list[str]:
    raw = str(settings.get("appeal_skip_keywords") or "")
    return [k.strip().lower() for k in raw.replace("\n", ",").split(",") if k.strip()]


def matching_keyword(text: str, keywords: list[str]) -> str:
    low = (text or "").lower()
    for k in keywords:
        if k in low:
            return k
    return ""


def filed_today(db: Session) -> int:
    """Auto-appeals filed since local midnight (the daily cap counter)."""
    midnight = timeutil.local_midnight_utc().replace(tzinfo=None)
    return (db.query(models.Appeal)
            .filter(models.Appeal.filed_by == "auto",
                    models.Appeal.submitted_at != None,           # noqa: E711
                    models.Appeal.submitted_at >= midnight).count())


# ---------------------------------------------------------------------------
# review info
# ---------------------------------------------------------------------------

def _join_reasons(reject_info) -> tuple[str, str]:
    """(reasons, suggestion) from a reject_info list as /ad/review_info/ and
    /adgroup/review_info/ return it: [{reasons[], suggestion, ...}]."""
    reasons: list[str] = []
    suggestions: list[str] = []
    for ri in reject_info or []:
        if not isinstance(ri, dict):
            continue
        for r in ri.get("reasons") or []:
            r = str(r or "").strip()
            if r and r not in reasons:
                reasons.append(r)
        s = str(ri.get("suggestion") or "").strip()
        if s and s not in suggestions:
            suggestions.append(s)
    return "; ".join(reasons), " ".join(suggestions)


def fetch_review(token: str, advertiser_id: str, adgroup_ids: list[str],
                 ad_ids: list[str]) -> tuple[dict, dict]:
    """Best-effort: (adgroup_review_map, ad_review_map). A failed call leaves
    the map empty — the appeal can still be filed, just without reasons."""
    groups: dict = {}
    ads: dict = {}
    if adgroup_ids:
        try:
            info = tiktok_api.get_adgroup_review_info(token, advertiser_id, adgroup_ids)
            groups = info.get("groups") or {}
        except tiktok_api.TikTokError as e:
            log.warning("adgroup review_info failed for %s: %s", advertiser_id, e)
    if ad_ids:
        try:
            ads = tiktok_api.get_ad_review_info(token, advertiser_id, ad_ids)
        except tiktok_api.TikTokError as e:
            log.warning("ad review_info failed for %s: %s", advertiser_id, e)
    return groups, ads


# ---------------------------------------------------------------------------
# filing
# ---------------------------------------------------------------------------

def file_appeal(db: Session, row: models.Appeal, token: str, settings: dict | None = None,
                *, filed_by: str = "auto", reason: str | None = None) -> bool:
    """POST /adgroup/appeal/ for one row. Returns True when TikTok accepted the
    request (status → appealing); on refusal the row goes to error with
    TikTok's message and attempts+1, so the operator sees exactly why."""
    settings = settings or get_settings(db)
    text = reason if reason is not None else render_reason(
        settings.get("appeal_reason", ""), ad_name=row.ad_name or "",
        campaign_name=row.campaign_name or "", reasons=row.reasons or "")
    row.appeal_reason = text
    row.filed_by = filed_by
    row.attempts = (row.attempts or 0) + 1
    try:
        data = tiktok_api.appeal_adgroup(token, row.advertiser_id, row.adgroup_id,
                                         ad_id=row.ad_id or None, appeal_reason=text)
    except tiktok_api.TikTokError as e:
        row.status = "error"
        row.error = f"{e.message} (code {e.code})"[:500]
        row.request_id = e.request_id or ""
        db.commit()
        return False
    row.status = "appealing"
    row.tiktok_status = "APPEALING"
    row.error = ""
    row.submitted_at = _now()
    row.checked_at = row.submitted_at
    if isinstance(data, dict):
        row.request_id = str(data.get("request_id") or row.request_id or "")
    db.commit()
    return True


def decide(row: models.Appeal, settings: dict, today: int) -> tuple[str, str]:
    """What the engine does with a rejection that has no appeal yet.
    Returns (action, why): file | skip | wait."""
    if not settings.get("appeal_auto_enabled"):
        return "wait", "auto-appeal is off"
    kw = matching_keyword((row.reasons or "") + " " + (row.suggestion or ""), skip_keywords(settings))
    if kw:
        return "skip", f"reason matches skip keyword “{kw}”"
    cap = int(settings.get("appeal_daily_cap") or 0)
    if cap and today >= cap:
        return "wait", f"daily cap of {cap} reached"
    return "file", ""


# ---------------------------------------------------------------------------
# the sync (called by the issue scan)
# ---------------------------------------------------------------------------

def _latest_row(db: Session, advertiser_id: str, adgroup_id: str) -> models.Appeal | None:
    return (db.query(models.Appeal)
            .filter(models.Appeal.advertiser_id == advertiser_id,
                    models.Appeal.adgroup_id == adgroup_id)
            .order_by(models.Appeal.id.desc()).first())


def sync(db: Session, rejected_ads: list[dict], scanned_advertisers: set[str] | None = None,
         settings: dict | None = None) -> dict:
    """rejected_ads: dicts from /ad/get/ plus 'advertiser_id', 'advertiser_name'
    and 'access_token'. Returns {(advertiser_id, ad_id): Appeal row} so the
    issue scan can show the real reason and appeal state next to each ad."""
    settings = settings or get_settings(db)
    now = _now()
    by_ad: dict[tuple[str, str], models.Appeal] = {}

    # 1. group by ad group ----------------------------------------------------
    groups: dict[tuple[str, str], dict] = {}
    for ad in rejected_ads:
        sec = str(ad.get("secondary_status") or "")
        if not is_appealable(sec):
            continue
        adv = str(ad.get("advertiser_id") or "")
        agid = str(ad.get("adgroup_id") or "")
        if not adv or not agid:
            continue
        g = groups.setdefault((adv, agid), {
            "token": ad.get("access_token") or "", "advertiser_name": ad.get("advertiser_name") or "",
            "campaign_id": str(ad.get("campaign_id") or ""), "campaign_name": str(ad.get("campaign_name") or ""),
            "ads": [], "group_denied": False})
        g["ads"].append(ad)
        if sec in tiktok_api.ADGROUP_REJECTED_STATUSES:
            g["group_denied"] = True

    # 2. review info per advertiser (reasons, verdict, appeal status) ----------
    by_adv: dict[str, list[tuple[str, dict]]] = {}
    for (adv, agid), g in groups.items():
        by_adv.setdefault(adv, []).append((agid, g))
    today = filed_today(db)
    for adv, items in by_adv.items():
        token = items[0][1]["token"]
        adgroup_ids = [agid for agid, _ in items]
        ad_ids = [str(a.get("ad_id")) for _, g in items for a in g["ads"]
                  if str(a.get("secondary_status")) in tiktok_api.AD_REJECTED_STATUSES]
        gmap, amap = fetch_review(token, adv, adgroup_ids, ad_ids) if token else ({}, {})

        for agid, g in items:
            ginfo = gmap.get(agid) or {}
            audit_time = str(ginfo.get("last_audit_time") or "")
            tiktok_status = str(ginfo.get("appeal_status") or "")
            # reasons: ad-level first, ad-group level as fallback
            reasons_l, sugg_l = [], []
            for a in g["ads"]:
                r, s = _join_reasons((amap.get(str(a.get("ad_id"))) or {}).get("reject_info"))
                if r and r not in reasons_l:
                    reasons_l.append(r)
                if s and s not in sugg_l:
                    sugg_l.append(s)
            if not reasons_l:
                r, s = _join_reasons(ginfo.get("reject_info"))
                if r:
                    reasons_l.append(r)
                if s:
                    sugg_l.append(s)
            reasons = "; ".join(reasons_l)[:1000]
            suggestion = " ".join(sugg_l)[:1000]
            names = [str(a.get("ad_name") or a.get("ad_id") or "") for a in g["ads"]]
            single_ad = g["ads"][0] if (len(g["ads"]) == 1 and not g["group_denied"]) else None

            row = _latest_row(db, adv, agid)
            # A NEW row only for a new rejection: none tracked yet, the last one
            # was cleared (the rejection went away, so this is a fresh one), or
            # TikTok's review time moved on. A finished row (failed/successful/
            # done/dismissed) for the SAME rejection is reused untouched — one
            # appeal per rejection, and a dismissal sticks until a new review.
            fresh = (row is None or row.status == "cleared" or bool(row.gone)
                     or (bool(audit_time) and bool(row.audit_time) and row.audit_time != audit_time))
            if fresh:
                row = models.Appeal(
                    advertiser_id=adv, advertiser_name=g["advertiser_name"],
                    campaign_id=g["campaign_id"], campaign_name=g["campaign_name"],
                    adgroup_id=agid, status="pending", filed_by="")
                db.add(row)
            row.ad_id = str(single_ad.get("ad_id")) if single_ad else ""
            row.ad_name = "; ".join(n for n in names if n)[:300]
            row.ads_n = len(g["ads"])
            row.rejected_status = ("AD_STATUS_ADGROUP_AUDIT_DENY" if g["group_denied"]
                                   else str(g["ads"][0].get("secondary_status") or ""))
            row.audit_time = audit_time or row.audit_time or ""
            if reasons:
                row.reasons = reasons
            if suggestion:
                row.suggestion = suggestion
            row.last_seen_at = now
            row.gone = False
            if ginfo.get("review_status"):
                row.review_status = str(ginfo.get("review_status"))

            # someone (Ads Manager) already appealed it — track, don't file again
            if tiktok_status in TIKTOK_TO_STATUS and row.status in ("pending", "skipped", "error"):
                row.status = TIKTOK_TO_STATUS[tiktok_status]
                row.tiktok_status = tiktok_status
                row.filed_by = row.filed_by or "ads-manager"
                row.checked_at = now
                if row.status in FINAL:
                    row.resolved_at = now
            elif row.status in ("pending", "skipped") or (
                    row.status == "error" and (row.attempts or 0) < 3 and _transient_error(row)):
                action, why = decide(row, settings, today)
                if action == "file" and token:
                    if file_appeal(db, row, token, settings, filed_by="auto"):
                        today += 1
                elif action == "skip":
                    row.status = "skipped"
                    row.error = why
                else:
                    if row.status != "error":
                        row.status = "pending"
                        row.error = why
            db.flush()
            for a in g["ads"]:
                by_ad[(adv, str(a.get("ad_id")))] = row
    db.commit()

    # 3. rejections that vanished from a successfully scanned account ----------
    #    open ones → cleared (nothing left to appeal); finished ones keep their
    #    outcome but are flagged gone, so a LATER rejection of the same ad group
    #    starts a new row instead of hiding behind the old verdict.
    if scanned_advertisers:
        seen_keys = set(groups.keys())
        for row in (db.query(models.Appeal)
                    .filter(models.Appeal.gone == False,                    # noqa: E712
                            models.Appeal.status != "cleared").all()):
            if row.advertiser_id not in scanned_advertisers or (row.advertiser_id, row.adgroup_id) in seen_keys:
                continue
            if row.status in ("pending", "skipped", "error"):
                row.status = "cleared"
                row.error = ""
                row.resolved_at = now
            elif row.status != "appealing":      # an appeal in flight keeps polling TikTok
                row.gone = True
        db.commit()

    # 4. refresh appeals waiting on TikTok ------------------------------------
    refresh(db, max_age_min=10)
    return by_ad


def _transient_error(row: models.Appeal) -> bool:
    e = (row.error or "").lower()
    return any(t in e for t in ("code 40100", "code 50000", "code http", "internal error",
                                 "try again", "timeout", "network"))


def refresh(db: Session, max_age_min: int = 10, only_ids: list[int] | None = None) -> int:
    """Pull appeal_status for every appealing row (older than max_age_min since
    its last check) from /adgroup/review_info/, 20 ad groups per call.
    Returns the number of rows whose status changed."""
    now = _now()
    cutoff = now - timedelta(minutes=max_age_min)
    q = db.query(models.Appeal).filter(models.Appeal.status == "appealing")
    if only_ids:
        q = q.filter(models.Appeal.id.in_(only_ids))
    rows = [r for r in q.all() if only_ids or not r.checked_at or r.checked_at <= cutoff]
    if not rows:
        return 0
    tokens = {a.advertiser_id: a.access_token for a in
              db.query(models.AdAccount).filter(
                  models.AdAccount.advertiser_id.in_({r.advertiser_id for r in rows})).all()}
    changed = 0
    by_adv: dict[str, list[models.Appeal]] = {}
    for r in rows:
        by_adv.setdefault(r.advertiser_id, []).append(r)
    for adv, group in by_adv.items():
        token = tokens.get(adv) or ""
        if not token:
            continue
        try:
            info = tiktok_api.get_adgroup_review_info(token, adv, [r.adgroup_id for r in group])
        except tiktok_api.TikTokError as e:
            log.warning("appeal status check failed for %s: %s", adv, e)
            continue
        gmap = info.get("groups") or {}
        for r in group:
            gi = gmap.get(r.adgroup_id) or {}
            r.checked_at = now
            ts = str(gi.get("appeal_status") or "")
            if ts:
                r.tiktok_status = ts
            if gi.get("review_status"):
                r.review_status = str(gi.get("review_status"))
            new = TIKTOK_TO_STATUS.get(ts)
            if new and new != r.status:
                r.status = new
                changed += 1
                if new in FINAL:
                    r.resolved_at = now
    db.commit()
    return changed


# ---------------------------------------------------------------------------
# page helpers
# ---------------------------------------------------------------------------

def summary(db: Session) -> dict:
    """Counts for the Appeals page tiles + Inbox."""
    rows = db.query(models.Appeal.status).all()
    counts: dict[str, int] = {}
    for (s,) in rows:
        counts[s] = counts.get(s, 0) + 1
    return {
        "open": counts.get("pending", 0) + counts.get("skipped", 0) + counts.get("error", 0),
        "pending": counts.get("pending", 0), "skipped": counts.get("skipped", 0),
        "error": counts.get("error", 0), "appealing": counts.get("appealing", 0),
        "won": counts.get("successful", 0) + counts.get("done", 0),
        "lost": counts.get("failed", 0), "cleared": counts.get("cleared", 0) + counts.get("dismissed", 0),
        "today": filed_today(db), "total": len(rows),
    }
