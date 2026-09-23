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


ACT_KINDS = ("pause", "flag", "would_pause", "held")
LOOKBACK_DAYS = {"today": (0, 1), "yesterday": (-1, 0), "3d": (-2, 1), "7d": (-6, 1)}


def lookback_bounds(key: str):
    """(start_utc, end_utc) of a profit-rule window, in the business timezone's days."""
    a, b = LOOKBACK_DAYS.get(key, LOOKBACK_DAYS["today"])
    return timeutil.local_midnight_utc(a), timeutil.local_midnight_utc(b)


def decide(mode: str, paused_last_hour: int, cap: int) -> str:
    """What a breach turns into (pure): pause | held (hourly cap) | flag | would_pause."""
    if mode == "dry_run":
        return "would_pause"
    if mode == "flag":
        return "flag"
    if cap and paused_last_hour >= cap:
        return "held"
    return "pause"


class _Pass:
    """One evaluation pass for one user: mode, hourly cap budget, one 'held' notice."""
    def __init__(self, db: Session, settings: dict, ids: set | None):
        self.mode = settings.get("rules_mode") or "pause"
        self.cap = int(settings.get("rules_hourly_cap") or 0)
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        q = db.query(models.RuleAction).filter(models.RuleAction.action == "pause", models.RuleAction.ok == True,  # noqa: E712
                                               models.RuleAction.created_at >= since)
        self.paused_last_hour = sum(1 for r in q if ids is None or r.advertiser_id in ids)
        self.held = 0


def _pause_campaign(db: Session, accounts: dict, rec: models.CampaignRecord,
                    rule: str, value: float, actions: list, run: "_Pass | None" = None):
    """Shared executor (metric and profit rules): pause — or, per the rails, flag it, log
    what it would do (dry run), or hold it past the hourly cap. Every outcome is a RuleAction."""
    kind = decide(run.mode, run.paused_last_hour, run.cap) if run is not None else "pause"
    if kind != "pause":
        action = models.RuleAction(advertiser_id=rec.advertiser_id, campaign_id=rec.campaign_id,
                                   campaign_name=rec.campaign_name, rule=rule, metric_value=value, action=kind, ok=True)
        if kind == "would_pause":
            action.detail = f"dry run — would have paused at {rule} (value {value:.2f}, spend ${rec.spend_today or 0:.2f})"
        elif kind == "flag":
            action.detail = f"flag-only — {rule} (value {value:.2f}); not paused"
            db.add(models.Alert(kind="rule_action", ref_id=rec.advertiser_id, level="info",
                                message=f"Rule flag: “{rec.campaign_name}” — {rule} (hit {value:.2f}). Rules are in flag-only mode, so it's still running.",
                                href=f"/status?state=all&open={rec.campaign_id}"))
        else:
            action.detail = f"held — the hourly cap of {run.cap} rule pauses was reached; not paused"
            run.held += 1
            if run.held == 1:
                db.add(models.Alert(kind="rule_action", ref_id=rec.advertiser_id, level="warn",
                                    message=f"Rules hit the cap of {run.cap} pauses in an hour — further breaches are held, not paused. "
                                            "Check Health › Automation; raise the cap in Settings › Rules if this is expected."))
        db.add(action)
        actions.append(action)
        return
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
        if run is not None:
            run.paused_last_hour += 1
        action.detail = f"paused at {rule} (value {value:.2f}, spend ${rec.spend_today:.2f})"
        db.add(models.Alert(
            kind="rule_action", ref_id=rec.advertiser_id, level="warn",
            message=f"Auto-paused “{rec.campaign_name}” — {rule} "
                    f"(hit {value:.2f} after ${rec.spend_today:.2f} spend)."))
        live_log.push("info", f"Rule engine paused {rec.campaign_name}: {rule}", advertiser_id=str(getattr(rec, "advertiser_id", "") or ""))
    except tiktok_api.TikTokError as e:
        action.ok = False
        action.detail = f"pause FAILED: code={e.code} {e.message}"
        db.add(models.Alert(
            kind="rule_action", ref_id=rec.advertiser_id, level="err",
            message=f"Rule engine tried to pause “{rec.campaign_name}” ({rule}) "
                    f"but TikTok refused (code {e.code}). Check it manually."))
    db.add(action)
    actions.append(action)


def _recently_paused(db: Session, campaign_id: str, now) -> bool:
    """A rule acts on a campaign at most once per business day (pause, flag, dry-run entry or
    hold), and never within RESUME_COOLDOWN of its last pause — a campaign the operator
    resumed isn't re-paused a minute later."""
    last = (db.query(models.RuleAction)
            .filter(models.RuleAction.campaign_id == campaign_id, models.RuleAction.action.in_(ACT_KINDS))
            .order_by(models.RuleAction.created_at.desc()).first())
    if last is None or not last.created_at:
        return False
    if last.action == "pause" and last.ok and (now - last.created_at) < RESUME_COOLDOWN:
        return True
    if last.action == "held":
        # held by the HOURLY cap: once the hour has moved on it gets its turn (not a whole day)
        return (now - last.created_at) < timedelta(hours=1)
    midnight = timeutil.local_midnight_utc(0).replace(tzinfo=None)
    return last.created_at >= midnight and (last.action != "pause" or last.ok)


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


def evaluate_pause_rules(db: Session, settings: dict | None = None, ids: set | None = None) -> list[models.RuleAction]:
    """`ids` = the advertiser ids these settings apply to (one user's accounts);
    None = every account (single-user installs, tests)."""
    settings = settings or get_settings(db)
    if not settings["rules_enabled"]:
        return []
    if ids is not None and not ids:
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
        pnl = pnl_data.source_pnl(db, start, end, ids)
        profitable_sources = {src for src, row in pnl.items() if row["profit"] > 0}

    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    run = _Pass(db, settings, ids)
    active = (db.query(models.CampaignRecord)
              .filter(models.CampaignRecord.operation_status == "ENABLE").all())
    for rec in active:
        if ids is not None and rec.advertiser_id not in ids:
            continue
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
        _pause_campaign(db, accounts, rec, rule, value, actions, run)
    db.commit()
    return actions


def losing_sources(pnl: dict, min_spend: float, loss_limit: float, roas_min: float) -> dict[str, str]:
    """{source: why} over the window (pure): losing more than loss_limit, or ROAS below
    roas_min — both only past min_spend."""
    out: dict[str, str] = {}
    for src, row in pnl.items():
        spend = float(row.get("spend") or 0)
        if spend < min_spend or spend <= 0:
            continue
        profit = float(row.get("profit") or 0)
        roas = float(row.get("revenue") or 0) / spend
        if loss_limit > 0 and profit <= -loss_limit:
            out[src] = f"source P&L < -{loss_limit:.2f} ({src})"
        elif roas_min > 0 and roas < roas_min:
            out[src] = f"source ROAS {roas:.2f} < {roas_min:.2f} ({src})"
    return out


def evaluate_profit_rules(db: Session, settings: dict | None = None, ids: set | None = None) -> list[models.RuleAction]:
    """Pause every campaign on a source that is losing more than profit_loss_limit — or whose
    ROAS is under profit_roas_min — over the chosen lookback (today / yesterday / 3 d / 7 d),
    after profit_min_spend of spend (revenue truth). With `ids` (one user's accounts) the
    P&L is that user's share of each source."""
    settings = settings or get_settings(db)
    if not settings.get("profit_rules_enabled"):
        return []
    if ids is not None and not ids:
        return []
    loss_limit = float(settings["profit_loss_limit"] or 0)
    min_spend = float(settings["profit_min_spend"] or 0)
    roas_min = float(settings.get("profit_roas_min") or 0)
    if loss_limit <= 0 and roas_min <= 0:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lookback = settings.get("profit_lookback") or "today"
    start, end = lookback_bounds(lookback)
    pnl = pnl_data.source_pnl(db, start, end, ids)
    losing = losing_sources(pnl, min_spend, loss_limit, roas_min)
    if not losing:
        return []
    camp_source = pnl_data.campaign_source_map(db)
    losing_campaigns = {cid for cid, src in camp_source.items() if src in losing}
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    run = _Pass(db, settings, ids)
    actions: list[models.RuleAction] = []
    active = (db.query(models.CampaignRecord)
              .filter(models.CampaignRecord.operation_status == "ENABLE",
                      models.CampaignRecord.campaign_id.in_(list(losing_campaigns) or [""])).all())
    for rec in active:
        if ids is not None and rec.advertiser_id not in ids:
            continue
        if _recently_paused(db, rec.campaign_id, now):
            continue
        src = camp_source.get(rec.campaign_id, "")
        row = pnl.get(src, {})
        why = losing.get(src, "")
        value = row.get("profit", 0.0) if "P&L" in why else (float(row.get("revenue") or 0) / float(row.get("spend") or 1))
        _pause_campaign(db, accounts, rec, why + ("" if lookback == "today" else f" · {lookback}"), value, actions, run)
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


def check_fresh_inventory(db: Session, settings: dict | None = None, user_id: int | None = None):
    """Alert (24h-repeat max) when never-launched account inventory runs low —
    per user (their own accounts, their own threshold) when `user_id` is given."""
    settings = settings or get_settings(db)
    minimum = int(settings.get("min_fresh_accounts") or 0)
    if minimum <= 0:
        return
    from .routes.super_launcher import eligible_accounts  # local import: no cycle at module load
    fresh = len(eligible_accounts(db, "new_only", 10_000, owner_user_id=user_id))
    if fresh >= minimum:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ref = "fresh" if user_id is None else f"fresh:u{user_id}"
    last = (db.query(models.Alert).filter_by(kind="inventory_low", ref_id=ref)
            .order_by(models.Alert.created_at.desc()).first())
    if last and last.created_at and (now - last.created_at) < timedelta(hours=24):
        return
    db.add(models.Alert(
        kind="inventory_low", ref_id=ref, level="warn",
        message=f"Only {fresh} fresh (never-launched) ad account(s) left "
                f"(threshold {minimum}). Time to source more accounts."))
    db.commit()


def check_pool_inventory(db: Session, settings: dict | None = None, user_id: int | None = None):
    """Alert (24h-repeat max) when the creative / identity / ad-text pools run
    low — but only for pools some preset actually uses, so automation never
    silently drains one and starts failing with CONFIG errors. Per user (their
    presets, their pools) when `user_id` is given."""
    import json as _json
    uses_library = uses_text_pool = False
    tq = db.query(models.Template)
    if user_id is not None:
        tq = tq.filter(models.Template.owner_user_id == user_id)
    for t in tq.all():
        try:
            blob = _json.loads(t.adgroup_settings or "{}")
        except (ValueError, TypeError):
            continue
        if blob.get("creative_source") == "library":
            uses_library = True
            if blob.get("ad_text_mode") == "pool":
                uses_text_pool = True
    checks = []
    if uses_library:
        checks.append(("creatives", models.Creative, "/creatives"))
    if uses_text_pool:
        checks.append(("ad texts", models.AdText, "/ad-texts"))
    if not checks:
        return
    low_water = 5
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for label, model, page in checks:
        q = db.query(model).filter_by(status="available")
        if user_id is not None:
            q = q.filter(model.owner_user_id == user_id)
        if model is models.Creative:
            q = q.filter_by(kind="video")       # images aren't launch inventory
        n = q.count()
        if n >= low_water:
            continue
        ref = label if user_id is None else f"{label}:u{user_id}"
        last = (db.query(models.Alert).filter_by(kind="pool_low", ref_id=ref)
                .order_by(models.Alert.created_at.desc()).first())
        if last and last.created_at and (now - last.created_at) < timedelta(hours=24):
            continue
        db.add(models.Alert(
            kind="pool_low", ref_id=ref, level="warn",
            message=f"Only {n} unused {label} left in the pool — launches will start "
                    f"refusing once it's empty. Refill on {page}."))
    db.commit()


# ---------------------------------------------------------------------------
# Auto top-ups
# ---------------------------------------------------------------------------

def evaluate_topups(db: Session, settings: dict | None = None, ids: set | None = None) -> list[models.TopUp]:
    """`ids` = the accounts these thresholds apply to (one user's); None = all."""
    settings = settings or get_settings(db)
    if not settings["topup_enabled"]:
        return []
    if ids is not None and not ids:
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
        if ids is not None and acct.advertiser_id not in ids:
            continue
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
            live_log.push("info", f"Auto top-up ${amount:.2f} → {acct.advertiser_id}", advertiser_id=str(acct.advertiser_id))
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
