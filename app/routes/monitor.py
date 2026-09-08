"""Health — one page for everything that can stop money flowing: a KPI strip
(blocked accounts, rejected ads, low wallets, API, auto-pauses), then tabs
that used to be separate pages: Issues (the unified inbox feed with fix
buttons), Balances (per BC wallet + runway), Automation (what the rule engine
did, with resume/undo) and System (sync + token health)."""
from __future__ import annotations

import json as _json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import balances, inbox as inbox_mod, models, queries, timeutil
from ..database import get_db
from ..templating import render

router = APIRouter()

VIEWS = ("issues", "balances", "automation", "system")


@router.get("/monitor")
def monitor(request: Request, db: Session = Depends(get_db)):
    from .. import rules as rules_mod
    from ..settings_store import get_settings
    from . import super_launcher as sl
    view = request.query_params.get("view", "issues")
    if view not in VIEWS:
        view = "issues"                       # old ?view=accounts → the Accounts page covers that now
    s = get_settings(db)
    bcs = (db.query(models.BusinessCenter).filter(models.BusinessCenter.status != "ACCESS_LOST")
           .order_by(models.BusinessCenter.name).all())
    accounts = (db.query(models.AdAccount).filter(models.AdAccount.status != "ACCESS_LOST")
                .order_by(models.AdAccount.advertiser_name).all())
    ctx = sl.account_picker_context(db, accounts)
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in bcs}
    blocked = [a for a in accounts if ctx["info"][a.advertiser_id]["state"] == "blocked"]
    blocked_by_bc: dict[str, int] = {}
    for a in blocked:
        k = bc_names.get(a.owner_bc_id or "", "no BC")
        blocked_by_bc[k] = blocked_by_bc.get(k, 0) + 1
    cooling = sum(1 for a in accounts if rules_mod.in_cooldown(a))

    # ---- issues = the unified feed ---------------------------------------------------------------
    items = inbox_mod.build(db)
    counts = inbox_mod.counts(items)
    kinds: dict[str, int] = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    for it in items:
        it["fix"] = _fix_actions(it)

    # ---- balances --------------------------------------------------------------------------------
    spend_by_aid = {r[0]: float(r[1] or 0) for r in db.query(models.CampaignRecord.advertiser_id, func.sum(models.CampaignRecord.spend_today)).group_by(models.CampaignRecord.advertiser_id)}
    balance_rows, seen = [], set()
    for bc in bcs:
        members = [a for a in accounts if a.owner_bc_id == bc.bc_id]
        seen.update(a.advertiser_id for a in members)
        in_acc = sum(float(a.balance or 0) for a in members)
        total = float(bc.balance or 0) + in_acc
        sp = sum(spend_by_aid.get(a.advertiser_id, 0.0) for a in members)
        low_accts = sum(1 for a in members if a.enabled and a.balance is not None and float(a.balance) < float(s["topup_below"] or 20))
        balance_rows.append({"name": bc.name or bc.bc_id, "bc_id": bc.bc_id, "currency": bc.currency or "USD",
                             "low": bc.balance is not None and bc.balance < balances.bc_threshold(bc), "threshold": balances.bc_threshold(bc),
                             "wallet": float(bc.balance or 0), "in_accounts": in_acc, "total": total, "accounts": len(members),
                             "spend_today": sp, "runway": (total / sp) if sp > 0 else None, "low_accounts": low_accts,
                             "portal": balances.bc_portal_url(bc.bc_id), "synced": bc.last_synced_at})
    orphans = [a for a in accounts if a.advertiser_id not in seen]
    if orphans:
        in_acc = sum(float(a.balance or 0) for a in orphans); sp = sum(spend_by_aid.get(a.advertiser_id, 0.0) for a in orphans)
        balance_rows.append({"name": "No Business Center", "bc_id": "", "currency": "USD", "low": False, "threshold": 0, "wallet": 0.0, "in_accounts": in_acc,
                             "total": in_acc, "accounts": len(orphans), "spend_today": sp, "runway": (in_acc / sp) if sp > 0 else None, "low_accounts": 0, "portal": "", "synced": None})
    btotals = {k: sum(r[k] for r in balance_rows) for k in ("wallet", "in_accounts", "total", "spend_today")}
    low_bcs = [r for r in balance_rows if r["low"]]

    # ---- automation ------------------------------------------------------------------------------
    actions = db.query(models.RuleAction).order_by(models.RuleAction.created_at.desc()).limit(150).all()
    topups = db.query(models.TopUp).order_by(models.TopUp.created_at.desc()).limit(60).all()
    paused_ids = {r.campaign_id for r in db.query(models.CampaignRecord.campaign_id).filter(models.CampaignRecord.operation_status == "DISABLE")}
    day_start = timeutil.local_midnight_utc(0).replace(tzinfo=None)
    today_pauses = sum(1 for a in actions if a.action == "pause" and a.ok and a.created_at and a.created_at >= day_start)
    today_topups = sum(float(t.amount or 0) for t in topups if t.ok and t.created_at and t.created_at >= day_start)
    still_paused = [a for a in actions if a.action == "pause" and a.ok and a.campaign_id in paused_ids]
    names = {a.advertiser_id: (a.advertiser_name or a.advertiser_id) for a in accounts}
    rule_bits = []
    if s["rule_cpm_max"]: rule_bits.append(f"CPM > ${s['rule_cpm_max']:.0f}")
    if s["rule_cpc_max"]: rule_bits.append(f"CPC > ${s['rule_cpc_max']:.2f}")
    if s["rule_cpa_max"]: rule_bits.append(f"CPA > ${s['rule_cpa_max']:.0f}")
    rules = [
        {"key": "rules", "on": bool(s["rules_enabled"]), "name": "Auto-pause on metrics", "tab": "rules",
         "sub": (", ".join(rule_bits) + f" after ${s['rule_min_spend']:.0f} spend") if rule_bits else "no thresholds set"},
        {"key": "profit", "on": bool(s["profit_rules_enabled"]), "name": "Auto-pause losing sources", "tab": "rules",
         "sub": f"losing more than ${s['profit_loss_limit']:.0f} today after ${s['profit_min_spend']:.0f} spend" + (" · profitable ones protected" if s["protect_profitable"] else "")},
        {"key": "topup", "on": bool(s["topup_enabled"]), "name": "Auto top-up from BC wallet", "tab": "rules",
         "sub": f"${s['topup_amount']:.0f} when an account drops under ${s['topup_below']:.0f} · cap ${s['topup_daily_cap']:.0f}/day"},
        {"key": "appeal", "on": bool(s["appeal_auto_enabled"]), "name": "Auto-appeal rejected ads", "tab": "appeals",
         "sub": f"up to {int(s['appeal_daily_cap'] or 0)} a day" + (" · skips: " + s["appeal_skip_keywords"] if s["appeal_skip_keywords"] else "")},
        {"key": "cooldown", "on": True, "name": "Account cooldown", "tab": "launch",
         "sub": f"after {int(s['account_error_threshold'] or 0)} failed launches, rest {int(s['cooldown_hours'] or 0)} h"},
    ]

    # ---- system ----------------------------------------------------------------------------------
    reports = []
    for key in ("sync_report", "balance_report"):
        raw = queries.get_setting(db, key, "")
        if raw:
            try:
                data = _json.loads(raw)
                if data.get("errors") or data.get("complete") is False:
                    reports.append({"key": key, **data})
            except _json.JSONDecodeError:
                pass
    token_ok = bool(queries.any_access_token(db))
    from .. import appeals as appeals_mod
    ap = appeals_mod.summary(db)
    running = db.query(func.count(models.Job.id)).filter(models.Job.status.in_(("queued", "running"))).scalar() or 0
    last_bal = max([r["synced"] for r in balance_rows if r["synced"]], default=None)
    return render(request, "monitor.html", {
        "title": "Health", "view": view,
        "strip": {"blocked": len(blocked), "blocked_by_bc": sorted(blocked_by_bc.items(), key=lambda kv: -kv[1])[:3], "cooling": cooling,
                  "rejected": ap["open"], "appealing": ap["appealing"], "won": ap["won"], "lost": ap["lost"],
                  "low_bcs": low_bcs, "n_bc": len(bcs), "token_ok": token_ok, "synced_ago": queries.campaigns_synced_ago(db),
                  "today_pauses": today_pauses, "today_topups": today_topups, "errors": counts["err"], "warns": counts["warn"]},
        "items": items, "counts": counts, "kinds": sorted(kinds.items(), key=lambda kv: -kv[1]),
        "balance_rows": balance_rows, "btotals": btotals, "last_bal": last_bal,
        "actions": actions, "topups": topups, "paused_ids": paused_ids, "names": names, "still_paused": still_paused, "rules": rules, "s": s,
        "sync_reports": reports, "token_ok": token_ok, "accounts_count": len(accounts), "running_jobs": running,
        "scanned_at": queries.get_setting(db, "issues_scanned_at", ""), "KIND_LABEL": KIND_LABEL,
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


KIND_LABEL = {"bc_low_balance": "Wallet low", "account_error": "Account error", "rule_action": "Rule fired", "inventory_low": "Inventory",
              "cta_fallback": "CTA fallback", "issue_payment": "Payment", "issue_account": "Account status", "issue_bc": "Business Center",
              "issue_campaign": "Campaign", "issue_ad": "Ad rejected", "issue_spark": "Spark code", "launch_failed": "Launch failed",
              "queue_failed": "Queue", "cooldown": "Cooling down", "unattributed": "Source lost", "nosource": "No source",
              "appeals_open": "Appeals", "appeal_result": "Appeal result", "token": "Not connected"}


def _fix_actions(it: dict) -> list[dict]:
    """The one or two buttons that actually fix an item — no generic 'View'."""
    k, href, ext = it["kind"], it.get("href") or "", it.get("external")
    acts: list[dict] = []
    if k == "bc_low_balance" or k == "issue_bc":
        acts.append({"label": "Top up ↗", "href": href, "ext": True}); acts.append({"label": "Balances", "href": "/monitor?view=balances"})
    elif k == "issue_ad" or k == "appeals_open":
        acts.append({"label": "Appeal", "href": "/appeals"})
    elif k == "appeal_result":
        acts.append({"label": "Appeals", "href": "/appeals"})
    elif k == "launch_failed":
        acts.append({"label": "Results", "href": href}); acts.append({"label": "Retry launch", "href": "/super-launcher"})
    elif k == "queue_failed":
        acts.append({"label": "Queue", "href": "/queue"})
    elif k in ("issue_account", "issue_payment", "account_error"):
        if ext and href:
            acts.append({"label": "Ads Manager ↗", "href": href, "ext": True})
        acts.append({"label": "Accounts", "href": "/accounts?state=blocked"})
    elif k == "issue_campaign":
        acts.append({"label": "Campaigns", "href": "/status?state=blocked"})
    elif k == "issue_spark":
        acts.append({"label": "Sparks", "href": "/spark-codes"})
    elif k == "cooldown":
        acts.append({"label": "Accounts", "href": "/accounts?state=blocked"})
    elif k == "inventory_low":
        acts.append({"label": "Connect BC", "href": "/accounts?connect=1"})
    elif k == "unattributed":
        acts.append({"label": "P&L", "href": "/pnl"})
    elif k == "nosource":
        acts.append({"label": "Source check", "href": "/campaigns/source-check"})
    elif k == "rule_action":
        acts.append({"label": "Automation", "href": "/monitor?view=automation"})
    elif k == "token":
        acts.append({"label": "Connect", "href": "/oauth/connect"})
    elif href:
        acts.append({"label": "Open ↗" if ext else "Open", "href": href, "ext": bool(ext)})
    return acts


@router.get("/monitor/data")
def monitor_data(db: Session = Depends(get_db)):
    """Small JSON the Health page polls: counts only (a full refresh reloads the page)."""
    items = inbox_mod.build(db)
    return JSONResponse({"counts": inbox_mod.counts(items), "synced_ago": queries.campaigns_synced_ago(db)})
