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

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models, queries, tiktok_api

BAD_STATUS_TOKENS = ("REJECT", "AUDIT_DENY", "SUSPEND", "PUNISH", "BANNED", "FROZEN")


def ads_manager_url(advertiser_id: str) -> str:
    """Deep link into Ads Manager for one account (verified aadvid format)."""
    return f"https://ads.tiktok.com/i18n/dashboard?aadvid={advertiser_id}"


def _status_is_bad(status: str) -> bool:
    s = (status or "").upper()
    return any(tok in s for tok in BAD_STATUS_TOKENS)


def scan(db: Session) -> dict:
    """Full issue sweep. Returns {issues, accounts_scanned}."""
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
        ids = [a.advertiser_id for a in accounts]
        for i in range(0, len(ids), 100):
            try:
                for info in tiktok_api.get_advertiser_info(token, ids[i:i + 100]):
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

    # --- rejected ads (most recent 100 per account, best-effort) --------------
    # Every rejected ad is handed to the appeals engine, which fetches TikTok's
    # real reasons (/ad/review_info/), files the appeal when auto-appeal is on
    # and tracks the answer — so the issue row can say what was done about it.
    rejected_ads: list[dict] = []
    scanned: set[str] = set()
    if token:
        for acct in accounts:
            if not acct.access_token:
                continue
            try:
                data = tiktok_api.list_ads(acct.access_token, acct.advertiser_id)
            except tiktok_api.TikTokError:
                continue
            scanned.add(acct.advertiser_id)
            for ad in data.get("list", []):
                sec = str(ad.get("secondary_status", "") or "")
                if not _status_is_bad(sec):
                    continue
                rejected_ads.append({**ad, "advertiser_id": acct.advertiser_id,
                                     "advertiser_name": names.get(acct.advertiser_id, acct.advertiser_id),
                                     "access_token": acct.access_token})
    appeal_by_ad: dict = {}
    try:
        from . import appeals
        appeal_by_ad = appeals.sync(db, rejected_ads, scanned)
    except Exception:  # the appeals step must never take the scan down
        import logging
        logging.getLogger("adops.issues").exception("appeals sync failed")
    for ad in rejected_ads:
        sec = str(ad.get("secondary_status", "") or "")
        row = appeal_by_ad.get((ad["advertiser_id"], str(ad.get("ad_id", ""))))
        reasons = (row.reasons if row else "") or ""
        state = ""
        if row:
            from .appeals import STATUS_LABELS
            state = STATUS_LABELS.get(row.status, row.status)
        found.append(models.Issue(
            category="ad", level="err",
            advertiser_id=ad["advertiser_id"], advertiser_name=ad["advertiser_name"],
            ref=str(ad.get("ad_id", "")),
            message=f"Ad “{(ad.get('ad_name') or '')[:40]}” rejected"
                    + (f": {reasons[:160]}" if reasons else " (no reason returned)")
                    + (f" — {state}." if state else "."),
            detail=f"secondary_status={sec}" + (f" reasons={reasons}" if reasons else "")
                   + (f" appeal={row.status}" if row else "")))

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
    db.query(models.Issue).delete()
    for issue in found:
        db.add(issue)
    queries.set_setting(db, "issues_scanned_at", datetime.now(timezone.utc).isoformat())
    db.commit()
    return {"issues": len(found), "accounts_scanned": len(accounts)}
