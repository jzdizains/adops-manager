"""Issue scan — pulls TikTok-side problems into one reviewable list.

What it looks for (best-effort; every finding keeps the RAW TikTok status so
the operator can act on exactly what TikTok said):
  bc       — Business Center not in an enabled state
  account  — ad account status not ENABLE (suspended / in review / punished),
             with TikTok's rejection reason when the API provides one
  payment  — account effectively out of funds (balance ≈ 0) while campaigns
             are ACTIVE — spend is being blocked by money, not delivery
  campaign — campaigns whose secondary status shows rejection/suspension
  ad       — rejected / audit-denied ads, with reject reasons when present
  spark    — spark codes whose launches failed resolution in the last 7 days

The scan REBUILDS the issues table each run (like the campaign cache), so a
fixed problem disappears on the next scan.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models, queries, tiktok_api

# Any secondary status carrying one of these means TikTok rejected / blocked it.
# PARTIALLY_APPROVED = AD_STATUS_REVIEW_PARTIALLY_APPROVED, "one or more ad
# creatives have been rejected" (Enumeration – Ad Status – Secondary Status).
BAD_STATUS_TOKENS = ("REJECT", "AUDIT_DENY", "SUSPEND", "PUNISH", "BANNED", "FROZEN", "PARTIALLY_APPROVED")

# the only ad fields the scan needs — keeps 1,000-row pages small
AD_SCAN_FIELDS = ["advertiser_id", "campaign_id", "campaign_name", "adgroup_id", "adgroup_name",
                  "ad_id", "ad_name", "secondary_status", "operation_status", "create_time", "modify_time"]


def ads_manager_url(advertiser_id: str) -> str:
    """Deep link into Ads Manager for one account (verified aadvid format)."""
    return f"https://ads.tiktok.com/i18n/dashboard?aadvid={advertiser_id}"


def _status_is_bad(status: str) -> bool:
    s = (status or "").upper()
    return any(tok in s for tok in BAD_STATUS_TOKENS)


def _parse_tt_time(s: str):
    """TikTok sends 'YYYY-MM-DD HH:MM:SS' (UTC). None when absent/unparseable."""
    s = str(s or "")[:19]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _ad_time(ad: dict):
    """When the ad was last touched (≈ when it was rejected): modify_time, else create_time."""
    return _parse_tt_time(ad.get("modify_time")) or _parse_tt_time(ad.get("create_time"))


def issue_key(category, advertiser_id, ref, detail) -> tuple:
    """What makes two scans' issues the same problem: category + account + object + TikTok's
    raw status (reasons and appeal state are left out — they change while it's the same issue)."""
    d = str(detail or "")
    status = d.split(" ", 1)[0] if d.startswith(("status=", "secondary_status=", "balance=")) else d[:80]
    if status.startswith("balance="):
        status = "balance"                      # the amount moves; "out of funds" is one issue
    return (category or "", advertiser_id or "", ref or "", status)


def issue_since(prior, status_changed, started, now):
    """The date an issue shows: the earliest of when an earlier scan first saw it, when the
    account's status changed to this (account issues) and when TikTok last touched the ad
    (rejected ads) — never later than now."""
    def naive(d):      # stored values are naive UTC; a freshly-set one can still be aware in memory
        return d.astimezone(timezone.utc).replace(tzinfo=None) if getattr(d, "tzinfo", None) else d
    now = naive(now)
    cands = [naive(d) for d in (prior, status_changed, started) if d is not None]
    return min(cands + [now]) if cands else now


def scan(db: Session, should_stop=None, on_progress=None) -> dict:
    """Full issue sweep. Returns {issues, accounts_scanned, ads_read, …, stopped}.
    should_stop() is polled between accounts (a cancelled background job);
    on_progress("12 of 286 accounts") keeps the running label fresh."""
    stopped = False
    token = queries.any_access_token(db)
    accounts = queries.enabled_accounts(db)
    found: list[models.Issue] = []

    # --- BC status -----------------------------------------------------------
    for bc in db.query(models.BusinessCenter).all():
        if bc.status and "ENABLE" not in bc.status.upper():
            found.append(models.Issue(
                category="bc", level="err", advertiser_id="", ref=bc.bc_id,
                advertiser_name=bc.name or bc.bc_id,
                message=f"Business Center “{bc.name or bc.bc_id}” is not active.",
                detail=f"status={bc.status}"))

    # --- account status + payment -------------------------------------------
    active_by_acct = {r[0] for r in
                      (db.query(models.CampaignRecord.advertiser_id)
                       .filter(models.CampaignRecord.operation_status == "ENABLE")
                       .distinct())}
    # refresh advertiser info (status + rejection reason) in one batch call
    info_by_id: dict[str, dict] = {}
    if token and accounts:
        # one batch per TikTok login (v116: several logins) — a login only sees its own accounts
        by_token: dict[str, list[str]] = {}
        for a in accounts:
            by_token.setdefault(a.access_token or token, []).append(a.advertiser_id)
        for tok, ids in by_token.items():
            for i in range(0, len(ids), 100):
                try:
                    for info in tiktok_api.get_advertiser_info(tok, ids[i:i + 100]):
                        info_by_id[str(info.get("advertiser_id", ""))] = info
                except tiktok_api.TikTokError:
                    pass

    for acct in accounts:
        info = info_by_id.get(acct.advertiser_id, {})
        status = str(info.get("status", acct.status) or "")
        if status:
            acct.status = status  # keep the cache fresh
        if status and "ENABLE" not in status.upper():
            reason = str(info.get("rejection_reason") or info.get("reason") or
                         info.get("status_reason") or "").strip()
            found.append(models.Issue(
                category="account", level="err",
                advertiser_id=acct.advertiser_id,
                advertiser_name=acct.advertiser_name or acct.advertiser_id,
                message=f"Account is {status.replace('STATUS_', '').replace('_', ' ').lower()}."
                        + (f" TikTok's reason: {reason}" if reason else ""),
                detail=f"status={status}" + (f" reason={reason}" if reason else "")))
        # payment: no money while campaigns are live
        if (acct.advertiser_id in active_by_acct
                and acct.balance is not None and acct.balance <= 1.0):
            found.append(models.Issue(
                category="payment", level="err",
                advertiser_id=acct.advertiser_id,
                advertiser_name=acct.advertiser_name or acct.advertiser_id,
                message=f"Out of funds (${(acct.balance or 0):.2f}) with ACTIVE campaigns — "
                        "delivery is blocked until it's topped up.",
                detail=f"balance={acct.balance}"))

    # --- campaign secondary status ------------------------------------------
    names = {a.advertiser_id: (a.advertiser_name or a.advertiser_id) for a in accounts}
    for rec in db.query(models.CampaignRecord).all():
        if _status_is_bad(rec.secondary_status):
            found.append(models.Issue(
                category="campaign", level="err",
                advertiser_id=rec.advertiser_id,
                advertiser_name=names.get(rec.advertiser_id, rec.advertiser_id),
                ref=rec.campaign_id,
                message=f"Campaign “{rec.campaign_name}” flagged: "
                        f"{rec.secondary_status.replace('CAMPAIGN_STATUS_', '').replace('_', ' ').lower()}.",
                detail=f"secondary_status={rec.secondary_status}"))

    # --- rejected ads (EVERY ad of every account) ------------------------------
    # Every rejected ad is handed to the appeals engine, which fetches TikTok's
    # real reasons (/ad/review_info/), files the appeal when auto-appeal is on
    # and tracks the answer — so the issue row can say what was done about it.
    # What each account returned (or why it failed) is kept as a ScanRun so the
    # Appeals page can show it.
    rejected_ads: list[dict] = []
    scanned: set[str] = set()
    run = models.ScanRun(accounts_total=0, accounts_ok=0, accounts_failed=0, ads_read=0, rejected_found=0,
                         status_counts="{}", errors="[]", duration_s=0.0)
    status_counts: dict[str, int] = {}
    errors: list[dict] = []
    t0 = time.monotonic()
    if token:
        with_token = [a for a in accounts if a.access_token]
        for i, acct in enumerate(with_token, 1):
            if should_stop and should_stop():
                stopped = True
                break
            if on_progress and (i == 1 or i % 5 == 0 or i == len(with_token)):
                on_progress(f"{i} of {len(with_token)} accounts")
            try:
                from . import background
                background.set_activity(f"issues.scan {i}/{len(with_token)} {acct.advertiser_name or acct.advertiser_id}")
            except Exception:  # noqa: BLE001
                pass
            run.accounts_total += 1
            # Stream the account's ads page by page (memory): a heavy account can have
            # up to 20,000 ads; loading them all at once was a transient spike that
            # OOM-killed the 512 MB box every slow sweep. We keep only the REJECTED ones,
            # and only the fields the appeal/inbox need — not the whole ad.
            def _take(rows, _acct=acct):
                for ad in rows:
                    sec = str(ad.get("secondary_status", "") or "")
                    run.ads_read += 1
                    status_counts[sec or "(none)"] = status_counts.get(sec or "(none)", 0) + 1
                    if not _status_is_bad(sec):
                        continue
                    run.rejected_found += 1
                    rejected_ads.append({
                        "ad_id": ad.get("ad_id", ""), "ad_name": ad.get("ad_name", ""),
                        "campaign_id": ad.get("campaign_id", ""), "campaign_name": ad.get("campaign_name", ""),
                        "adgroup_id": ad.get("adgroup_id", ""),
                        "secondary_status": sec, "operation_status": ad.get("operation_status", ""),
                        "create_time": ad.get("create_time", ""), "modify_time": ad.get("modify_time", ""),
                        "advertiser_id": _acct.advertiser_id,
                        "advertiser_name": names.get(_acct.advertiser_id, _acct.advertiser_id),
                        "access_token": _acct.access_token})
            try:
                tiktok_api.list_all_ads(acct.access_token, acct.advertiser_id, fields=AD_SCAN_FIELDS, on_page=_take)
            except tiktok_api.TikTokError as e:
                run.accounts_failed += 1
                if len(errors) < 50:
                    errors.append({"advertiser_id": acct.advertiser_id,
                                   "name": acct.advertiser_name or acct.advertiser_id, "error": str(e)[:200]})
                continue
            run.accounts_ok += 1
            scanned.add(acct.advertiser_id)
    run.status_counts = json.dumps(status_counts, sort_keys=True)
    run.errors = json.dumps(errors)
    run.duration_s = round(time.monotonic() - t0, 1)
    db.add(run)
    # keep the last 30 runs
    old = db.query(models.ScanRun).order_by(models.ScanRun.id.desc()).offset(30).all()
    for o in old:
        db.delete(o)
    appeal_by_ad: dict = {}
    try:
        from . import appeals
        appeal_by_ad = appeals.sync(db, rejected_ads, scanned)
    except Exception:  # the appeals step must never take the scan down
        import logging
        logging.getLogger("adops.issues").exception("appeals sync failed")
    # Recency window: drop rejected-ad issues whose ad was last changed more than
    # issue_max_age_days ago, so weeks-old rejections don't stack up in the feed forever
    # (0 = keep all). The scan rebuilds issues every run, so an aged-out one just stops
    # reappearing on the next scan. Appeals are unaffected — only the display is trimmed.
    from .settings_store import get_settings
    try:
        _max_age = int(get_settings(db).get("issue_max_age_days", 3) or 0)
    except Exception:  # noqa: BLE001
        _max_age = 3
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=_max_age)) if _max_age > 0 else None
    for ad in rejected_ads:
        if cutoff is not None:
            ts = _ad_time(ad)
            if ts is not None and ts < cutoff:
                continue      # older than the window — don't surface it
        sec = str(ad.get("secondary_status", "") or "")
        row = appeal_by_ad.get((ad["advertiser_id"], str(ad.get("ad_id", ""))))
        reasons = (row.reasons if row else "") or ""
        state = ""
        if row:
            from .appeals import STATUS_LABELS
            state = STATUS_LABELS.get(row.status, row.status)
        issue = models.Issue(
            category="ad", level="err",
            advertiser_id=ad["advertiser_id"], advertiser_name=ad["advertiser_name"],
            ref=str(ad.get("ad_id", "")),
            message=f"Ad “{(ad.get('ad_name') or '')[:40]}” rejected"
                    + (f": {reasons[:160]}" if reasons else " (no reason returned)")
                    + (f" — {state}." if state else "."),
            detail=f"secondary_status={sec}" + (f" reasons={reasons}" if reasons else "")
                   + (f" appeal={row.status}" if row else ""))
        issue._started = _ad_time(ad)           # TikTok's last touch ≈ when it was rejected
        found.append(issue)

    # --- spark resolution failures (last 7 days) ------------------------------
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)
    spark_fail_logs = (db.query(models.LaunchLog)
                       .filter(models.LaunchLog.ok == False,          # noqa: E712
                               models.LaunchLog.error_code == "SPARK",
                               models.LaunchLog.created_at >= week_ago).all())
    seen_sparks = set()
    for log in spark_fail_logs:
        key = log.spark_code_id or log.error_message[:60]
        if key in seen_sparks:
            continue
        seen_sparks.add(key)
        spark = db.get(models.SparkCode, log.spark_code_id) if log.spark_code_id else None
        found.append(models.Issue(
            category="spark", level="warn",
            advertiser_id=log.advertiser_id,
            advertiser_name=names.get(log.advertiser_id, log.advertiser_id),
            ref=str(log.spark_code_id or ""),
            message=f"Spark “{(spark.name if spark else '') or 'code'}” failed to resolve on a "
                    "launch — the auth code may be expired or the creator disconnected.",
            detail=log.error_technical[:300]))

    # --- rebuild the table ----------------------------------------------------
    if stopped:
        # a partial sweep must not wipe issues for the accounts it never reached
        db.commit()
        return {"issues": len(found), "accounts_scanned": run.accounts_total, "stopped": True,
                "ads_read": run.ads_read, "accounts_ok": run.accounts_ok, "accounts_failed": run.accounts_failed,
                "rejected_found": run.rejected_found}
    # keep each issue's first-seen date across rescans: the table is rebuilt, but a problem
    # that was already there is the SAME problem — dated when it started, not "3 min ago"
    prior = {issue_key(i.category, i.advertiser_id, i.ref, i.detail): i.detected_at
             for i in db.query(models.Issue.category, models.Issue.advertiser_id, models.Issue.ref,
                               models.Issue.detail, models.Issue.detected_at)}
    acct_changed = {a.advertiser_id: a.status_changed_at for a in accounts}
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    for issue in found:
        issue.detected_at = issue_since(prior.get(issue_key(issue.category, issue.advertiser_id, issue.ref, issue.detail)),
                                        acct_changed.get(issue.advertiser_id) if issue.category == "account" else None,
                                        getattr(issue, "_started", None), now_naive)
    db.query(models.Issue).delete()
    for issue in found:
        db.add(issue)
    queries.set_setting(db, "issues_scanned_at", datetime.now(timezone.utc).isoformat())
    db.commit()
    return {"issues": len(found), "accounts_scanned": len(accounts), "stopped": False,
            "ads_read": run.ads_read, "accounts_ok": run.accounts_ok, "accounts_failed": run.accounts_failed,
            "rejected_found": run.rejected_found}
