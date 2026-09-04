"""Unified notification inbox — ONE source of truth for everything that needs
the operator's attention. Feeds the bell dropdown, the /inbox page and the
Home "Needs attention" panel, so nothing is scattered across pages anymore.

Sources folded in (each becomes an `item` with a stable id):
  alert:<id>    Alert rows (BC low balance, account errors, rule actions, info)
  issue:<id>    Issue rows from the TikTok-side scan (payment, status, rejected ads…)
  launch:<ref>  failed launches (one per batch, last 24h)
  queue         queued launches that failed
  cooldown      accounts cooling down after launch failures
  token         TikTok not connected

Item shape: {id, kind, level(err|warn|info), title, message, href, external,
             at (datetime|None), ack (bool: can be dismissed), where}
Ordering: errors first, then warnings, then info; newest first within a level.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import balances, models, queries, timeutil

LEVEL_RANK = {"err": 0, "warn": 1, "info": 2}


def _alert_href(a: models.Alert) -> tuple[str, bool]:
    if a.kind == "bc_low_balance" and a.ref_id:
        return balances.bc_portal_url(a.ref_id), True
    if a.kind == "rule_action":
        return "/automation", False
    if a.kind in ("account_error", "inventory_low"):
        return "/monitor", False
    return "", False


def build(db: Session) -> list[dict]:
    items: list[dict] = []

    # ---- 1. Alert rows (dismissable) -----------------------------------------
    for a in balances.unacknowledged(db, limit=200):
        href, external = _alert_href(a)
        items.append({
            "id": f"alert:{a.id}", "kind": a.kind, "level": a.level or "warn",
            "title": {"bc_low_balance": "Wallet low", "account_error": "Account error",
                      "rule_action": "Rule fired", "inventory_low": "Inventory low",
                      "cta_fallback": "Auto CTA fallback"}.get(a.kind, "Notice"),
            "message": a.message, "href": href, "external": external,
            "at": a.created_at, "ack": True, "where": "",
        })

    # ---- 2. TikTok-side issues (cleared by the next scan, link to Health) ----
    for i in db.query(models.Issue).order_by(models.Issue.detected_at.desc()).all():
        if i.category == "bc" and i.ref:
            href, external = balances.bc_portal_url(i.ref), True
        elif i.category == "ad":
            href, external = "/appeals", False
        elif i.advertiser_id:
            href, external = f"https://ads.tiktok.com/i18n/dashboard?aadvid={i.advertiser_id}", True
        else:
            href, external = "/monitor?view=issues", False
        items.append({
            "id": f"issue:{i.id}", "kind": f"issue_{i.category}", "level": i.level or "err",
            "title": {"payment": "Payment", "account": "Account status", "bc": "Business Center",
                      "campaign": "Campaign", "ad": "Ad rejected", "spark": "Spark code"}.get(i.category, "Issue"),
            "message": i.message, "href": href, "external": external,
            "at": i.detected_at, "ack": False, "where": i.advertiser_name or "",
        })

    # ---- 3. failed launches, last 24h, one item per batch --------------------
    since = timeutil.now_utc().replace(tzinfo=None) - timedelta(hours=24)
    seen_batches: set[str] = set()
    for log in (db.query(models.LaunchLog)
                .filter(models.LaunchLog.ok == False,          # noqa: E712
                        models.LaunchLog.created_at >= since)
                .order_by(models.LaunchLog.created_at.desc()).limit(300)):
        ref = log.batch_ref or f"log{log.id}"
        if ref in seen_batches:
            continue
        seen_batches.add(ref)
        n = (db.query(func.count(models.LaunchLog.id))
             .filter(models.LaunchLog.batch_ref == log.batch_ref,
                     models.LaunchLog.ok == False).scalar() or 1) if log.batch_ref else 1  # noqa: E712
        items.append({
            "id": f"launch:{ref}", "kind": "launch_failed", "level": "err",
            "title": "Launch failed",
            "message": (f"{n} account(s) failed in batch {log.batch_ref}: " if log.batch_ref else "")
                       + (log.error_message or "unknown error")[:140],
            "href": f"/campaigns/result/{log.batch_ref}" if log.batch_ref else "/queue",
            "external": False, "at": log.created_at, "ack": False,
            "where": log.advertiser_name or log.advertiser_id or "",
        })

    # ---- 4. queued launches that failed --------------------------------------
    failed_q = (db.query(func.count(models.LaunchQueueItem.id))
                .filter(models.LaunchQueueItem.status == "failed").scalar() or 0)
    if failed_q:
        items.append({"id": "queue", "kind": "queue_failed", "level": "err",
                      "title": "Launch queue", "message": f"{failed_q} queued launch(es) failed",
                      "href": "/queue", "external": False, "at": None, "ack": False, "where": ""})

    # ---- 5. accounts cooling down --------------------------------------------
    from . import rules as rules_mod
    cooling = sum(1 for a in db.query(models.AdAccount).all() if rules_mod.in_cooldown(a))
    if cooling:
        items.append({"id": "cooldown", "kind": "cooldown", "level": "warn",
                      "title": "Cooling down",
                      "message": f"{cooling} account(s) cooling down after launch failures",
                      "href": "/monitor", "external": False, "at": None, "ack": False, "where": ""})

    # ---- 6. postbacks arriving WITHOUT a source (source lost before Glitchy) --
    from .routes.postback import UNATTRIBUTED
    lost = (db.query(func.count(models.PostbackEvent.id), func.coalesce(func.sum(models.PostbackEvent.revenue), 0.0))
            .filter(models.PostbackEvent.source == UNATTRIBUTED,
                    models.PostbackEvent.created_at >= since).one())
    if lost[0]:
        items.append({"id": "unattributed", "kind": "unattributed", "level": "err",
                      "title": "Source not reaching Glitchy",
                      "message": (f"{lost[0]} postback(s) (${float(lost[1] or 0):.2f}) arrived in the last 24h with an EMPTY "
                                  "{source} — the click that hit Glitchy had no ?source=. Check the prelander/lander "
                                  "forwards the query string to the offer link, and that the launch's landing URL carries it."),
                      "href": "/pnl", "external": False, "at": None, "ack": False, "where": ""})

    # ---- 6b. running campaigns launched WITHOUT a source (pre-source-wiring) --
    live_ids = {(r.advertiser_id, r.campaign_id) for r in
                db.query(models.CampaignRecord).filter(models.CampaignRecord.operation_status == "ENABLE")}
    nosrc = set()
    for lg in (db.query(models.LaunchLog).filter(models.LaunchLog.ok == True,          # noqa: E712
                                                 models.LaunchLog.campaign_id != "",
                                                 models.LaunchLog.source == "")):
        if (lg.advertiser_id, lg.campaign_id) in live_ids:
            nosrc.add(lg.campaign_id)
    if nosrc:
        items.append({"id": "nosource", "kind": "nosource", "level": "err",
                      "title": "Running campaigns without a source",
                      "message": (f"{len(nosrc)} running campaign(s) were launched with no ?source= on the landing URL — "
                                  "their clicks reach Glitchy unattributed. Source check reads the live URLs and can fix them."),
                      "href": "/campaigns/source-check", "external": False, "at": None, "ack": False, "where": ""})

    # ---- 6c. ad-rejection appeals ---------------------------------------------
    ap_open = (db.query(func.count(models.Appeal.id))
               .filter(models.Appeal.status.in_(("pending", "skipped", "error"))).scalar() or 0)
    if ap_open:
        items.append({"id": "appeals-open", "kind": "appeals_open", "level": "warn",
                      "title": "Rejected ads not appealed",
                      "message": f"{ap_open} rejected ad group(s) have no appeal on file — auto-appeal is off, "
                                 "a skip keyword matched, or TikTok refused the request. Review and appeal by hand.",
                      "href": "/appeals", "external": False, "at": None, "ack": False, "where": ""})
    for row in (db.query(models.Appeal)
                .filter(models.Appeal.status.in_(("failed", "successful", "done")),
                        models.Appeal.resolved_at >= since)
                .order_by(models.Appeal.resolved_at.desc()).limit(50)):
        lost = row.status == "failed"
        items.append({"id": f"appeal:{row.id}", "kind": "appeal_result", "level": "err" if lost else "info",
                      "title": "Appeal rejected" if lost else ("Appeal accepted" if row.status == "successful" else "Ad re-reviewed"),
                      "message": (f"“{(row.ad_name or row.adgroup_id)[:60]}” in {row.campaign_name or row.adgroup_id}: "
                                  + ("TikTok upheld the rejection" if lost
                                     else (f"review status now {row.review_status}" if row.review_status and row.status == "done"
                                           else "TikTok accepted the appeal — the ad is being re-reviewed"))),
                      "href": "/appeals", "external": False, "at": row.resolved_at, "ack": False,
                      "where": row.advertiser_name or ""})

    # ---- 7. TikTok not connected ---------------------------------------------
    if not queries.any_access_token(db):
        items.append({"id": "token", "kind": "token", "level": "err",
                      "title": "Not connected",
                      "message": "TikTok isn't connected — nothing can sync or launch",
                      "href": "/oauth/connect", "external": False, "at": None, "ack": False, "where": ""})

    # ---- order: severity, then newest ----------------------------------------
    def _key(it):
        at = it["at"]
        ts = at.timestamp() if at else 0
        return (LEVEL_RANK.get(it["level"], 2), -ts)
    items.sort(key=_key)
    return items


def counts(items: list[dict]) -> dict:
    return {"total": len(items),
            "err": sum(1 for i in items if i["level"] == "err"),
            "warn": sum(1 for i in items if i["level"] == "warn"),
            "info": sum(1 for i in items if i["level"] == "info")}


def serialize(it: dict) -> dict:
    """JSON-safe copy for the bell poller."""
    return {"id": it["id"], "kind": it["kind"], "level": it["level"], "title": it["title"],
            "message": it["message"], "href": it["href"], "external": it["external"],
            "ack": it["ack"], "where": it["where"],
            "at": (it["at"].isoformat() + "Z") if it["at"] else ""}
