"""The rule engine: auto-pause campaigns whose CPM/CPC/CPA exceed the
operator's thresholds (Settings page), and auto top-up account balances from
the BC wallet. Every action is logged (RuleAction / TopUp) and raises an
in-app alert. Guards:

  * min-spend: no rule judges a campaign below `rule_min_spend` today
  * pause-once: a campaign the engine paused isn't re-paused (and if the
    operator manually resumes it, a 6h cooldown stops instant re-pausing)
  * top-up daily cap per account, and the BC wallet must retain the amount
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import live_log, models, pnl_data, timeutil, tiktok_api
from .settings_store import get_settings

RESUME_COOLDOWN = timedelta(hours=6)


def _pause_campaign(db: Session, accounts: dict, rec: models.CampaignRecord,
                    rule: str, value: float, actions: list):
    """Shared pause executor: API call + RuleAction + Alert (used by both
    metric and profit rules)."""
    acct = accounts.get(rec.advertiser_id)
    action = models.RuleAction(
        advertiser_id=rec.advertiser_id, campaign_id=rec.campaign_id,
        campaign_name=rec.campaign_name, rule=rule, metric_value=value,
        action="pause")
    try:
        if not acct or not acct.access_token:
            raise tiktok_api.TikTokError("APP", "no token for advertiser")
        tiktok_api.update_campaign_status(
            acct.access_token, rec.advertiser_id, [rec.campaign_id], "DISABLE")
        rec.operation_status = "DISABLE"
        action.ok = True
        action.detail = f"paused at {rule} (value {value:.2f}, spend ${rec.spend_today:.2f})"
        db.add(models.Alert(
            kind="rule_action", ref_id=rec.campaign_id, level="warn",
            message=f"Auto-paused “{rec.campaign_name}” — {rule} "
                    f"(hit {value:.2f} after ${rec.spend_today:.2f} spend)."))
        live_log.push("info", f"Rule engine paused {rec.campaign_name}: {rule}")
    except tiktok_api.TikTokError as e:
        action.ok = False
        action.detail = f"pause FAILED: code={e.code} {e.message}"
        db.add(models.Alert(
            kind="rule_action", ref_id=rec.campaign_id, level="err",
            message=f"Rule engine tried to pause “{rec.campaign_name}” ({rule}) "
                    f"but TikTok refused (code {e.code}). Check it manually."))
    db.add(action)
    actions.append(action)


def _recently_paused(db: Session, campaign_id: str, now) -> bool:
    last = (db.query(models.RuleAction)
            .filter_by(campaign_id=campaign_id, action="pause", ok=True)
            .order_by(models.RuleAction.created_at.desc()).first())
    return bool(last and last.created_at and (now - last.created_at) < RESUME_COOLDOWN)


# ---------------------------------------------------------------------------
# Auto-pause
# ---------------------------------------------------------------------------

def _breached(settings: dict, rec: models.CampaignRecord) -> tuple[str, float] | None:
    checks = [
        ("cpm", settings["rule_cpm_max"], rec.cpm),
        ("cpc", settings["rule_cpc_max"], rec.cpc),
        ("cpa", settings["rule_cpa_max"], rec.cpa),
    ]
    for name, limit, value in checks:
        if limit and value and value > limit:
            return (f"{name} > {limit:.2f}", float(value))
    return None


def evaluate_pause_rules(db: Session, settings: dict | None = None) -> list[models.RuleAction]:
    settings = settings or get_settings(db)
    if not settings["rules_enabled"]:
        return []
    min_spend = float(settings["rule_min_spend"] or 0)
    actions: list[models.RuleAction] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # protect-profitable shield: sources in profit TODAY are exempt from metric rules
    profitable_sources: set[str] = set()
    camp_source: dict[str, str] = {}
    if settings.get("protect_profitable"):
        start, end = timeutil.range_bounds("today")
        camp_source = pnl_data.campaign_source_map(db)
        pnl = pnl_data.source_pnl(db, start, end)
        profitable_sources = {src for src, row in pnl.items() if row["profit"] > 0}

    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    active = (db.query(models.CampaignRecord)
              .filter(models.CampaignRecord.operation_status == "ENABLE").all())
    for rec in active:
        if (rec.spend_today or 0) < min_spend:
            continue
        breach = _breached(settings, rec)
        if not breach:
            continue
        if camp_source.get(rec.campaign_id) in profitable_sources:
            continue  # in profit today — metric rules stand down
        if _recently_paused(db, rec.campaign_id, now):
            continue
        rule, value = breach
        _pause_campaign(db, accounts, rec, rule, value, actions)
    db.commit()
    return actions


def evaluate_profit_rules(db: Session, settings: dict | None = None) -> list[models.RuleAction]:
    """Pause every campaign on a source whose P&L today is worse than
    -profit_loss_limit after profit_min_spend of spend (revenue truth)."""
    settings = settings or get_settings(db)
    if not settings.get("profit_rules_enabled"):
        return []
    loss_limit = float(settings["profit_loss_limit"] or 0)
    min_spend = float(settings["profit_min_spend"] or 0)
    if loss_limit <= 0:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start, end = timeutil.range_bounds("today")
    pnl = pnl_data.source_pnl(db, start, end)
    losing = {src for src, row in pnl.items()
              if row["spend"] >= min_spend and row["profit"] <= -loss_limit}
    if not losing:
        return []
    camp_source = pnl_data.campaign_source_map(db)
    losing_campaigns = {cid for cid, src in camp_source.items() if src in losing}
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    actions: list[models.RuleAction] = []
    active = (db.query(models.CampaignRecord)
              .filter(models.CampaignRecord.operation_status == "ENABLE",
                      models.CampaignRecord.campaign_id.in_(list(losing_campaigns) or [""])).all())
    for rec in active:
        if _recently_paused(db, rec.campaign_id, now):
            continue
        src = camp_source.get(rec.campaign_id, "")
        profit = pnl.get(src, {}).get("profit", 0.0)
        _pause_campaign(db, accounts, rec,
                        f"source P&L < -{loss_limit:.2f} ({src})", profit, actions)
    db.commit()
    return actions


# ---------------------------------------------------------------------------
# Account lifecycle: error cooldowns + fresh-inventory alert
# ---------------------------------------------------------------------------

def record_launch_outcome(db: Session, acct: models.AdAccount, ok: bool,
                          settings: dict | None = None):
    """Track consecutive launch failures per account; cool the account down
    once it crosses the threshold (auto-pick will skip it)."""
    settings = settings or get_settings(db)
    if ok:
        acct.error_count = 0
        return
    acct.error_count = (acct.error_count or 0) + 1
    threshold = int(settings.get("account_error_threshold") or 3)
    if acct.error_count >= threshold and not in_cooldown(acct):
        hours = int(settings.get("cooldown_hours") or 48)
        acct.cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(tzinfo=None)
        db.add(models.Alert(
            kind="account_error", ref_id=acct.advertiser_id, level="warn",
            message=f"Account {acct.advertiser_name or acct.advertiser_id} hit "
                    f"{acct.error_count} launch failures in a row — cooled down for {hours}h "
                    "(auto-pick will skip it)."))


def in_cooldown(acct: models.AdAccount) -> bool:
    if not acct.cooldown_until:
        return False
    return acct.cooldown_until > datetime.now(timezone.utc).replace(tzinfo=None)


def check_fresh_inventory(db: Session, settings: dict | None = None):
    """Alert (24h-repeat max) when never-launched account inventory runs low."""
    settings = settings or get_settings(db)
    minimum = int(settings.get("min_fresh_accounts") or 0)
    if minimum <= 0:
        return
    from .routes.super_launcher import eligible_accounts  # local import: no cycle at module load
    fresh = len(eligible_accounts(db, "new_only", 10_000))
    if fresh >= minimum:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = (db.query(models.Alert).filter_by(kind="inventory_low", ref_id="fresh")
            .order_by(models.Alert.created_at.desc()).first())
    if last and last.created_at and (now - last.created_at) < timedelta(hours=24):
        return
    db.add(models.Alert(
        kind="inventory_low", ref_id="fresh", level="warn",
        message=f"Only {fresh} fresh (never-launched) ad account(s) left "
                f"(threshold {minimum}). Time to source more accounts."))
    db.commit()


# ---------------------------------------------------------------------------
# Auto top-ups
# ---------------------------------------------------------------------------

def evaluate_topups(db: Session, settings: dict | None = None) -> list[models.TopUp]:
    settings = settings or get_settings(db)
    if not settings["topup_enabled"]:
        return []
    below = float(settings["topup_below"] or 0)
    amount = float(settings["topup_amount"] or 0)
    cap = float(settings["topup_daily_cap"] or 0)
    if amount <= 0 or below <= 0:
        return []
    today = timeutil.local_date_str()
    results: list[models.TopUp] = []
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}

    for acct in (db.query(models.AdAccount)
                 .filter(models.AdAccount.enabled == True).all()):  # noqa: E712
        if not acct.owner_bc_id or acct.balance is None or acct.balance >= below:
            continue
        bc = bcs.get(acct.owner_bc_id)
        if not bc:
            continue
        # daily cap per account
        given_today = sum(t.amount for t in db.query(models.TopUp)
                          .filter_by(advertiser_id=acct.advertiser_id, day=today, ok=True))
        if cap and given_today + amount > cap:
            continue
        # BC wallet must actually hold the amount
        if (bc.balance or 0) < amount:
            db.add(models.Alert(
                kind="bc_low_balance", ref_id=bc.bc_id, level="err",
                message=f"Auto top-up skipped: BC “{bc.name or bc.bc_id}” wallet "
                        f"({bc.currency} {bc.balance:.2f}) can't cover a "
                        f"${amount:.2f} transfer to {acct.advertiser_name or acct.advertiser_id}."))
            continue
        topup = models.TopUp(bc_id=bc.bc_id, advertiser_id=acct.advertiser_id,
                             amount=amount, day=today)
        prev_balance = float(acct.balance or 0)
        try:
            tiktok_api.bc_transfer(acct.access_token, bc.bc_id,
                                   acct.advertiser_id, amount, "RECHARGE")
            topup.ok = True
            topup.detail = f"balance was {prev_balance:.2f} (< {below:.2f})"
            acct.balance = prev_balance + amount
            bc.balance = (bc.balance or 0) - amount
            db.add(models.Alert(
                kind="rule_action", ref_id=acct.advertiser_id, level="info",
                message=f"Auto top-up: ${amount:.2f} → "
                        f"{acct.advertiser_name or acct.advertiser_id} "
                        f"(was ${prev_balance:.2f})."))
            live_log.push("info", f"Auto top-up ${amount:.2f} → {acct.advertiser_id}")
        except tiktok_api.TikTokError as e:
            topup.ok = False
            topup.detail = f"transfer FAILED: code={e.code} {e.message}"
            db.add(models.Alert(
                kind="rule_action", ref_id=acct.advertiser_id, level="err",
                message=f"Auto top-up to {acct.advertiser_name or acct.advertiser_id} "
                        f"failed (code {e.code}): {e.message[:120]}"))
        db.add(topup)
        results.append(topup)
    db.commit()
    return results
