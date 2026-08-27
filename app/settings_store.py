"""Runtime-tunable settings, stored in the Setting KV table so the operator
can change them on the /settings page without redeploying. The background
worker re-reads them every sweep, so changes apply within a minute."""
from __future__ import annotations

import json
import secrets

from sqlalchemy.orm import Session

from . import models

KEY = "app_settings"

DEFAULTS: dict = {
    # --- automation rules (auto-pause) ---------------------------------------
    "rules_enabled": False,        # master switch — OFF until the operator opts in
    "rule_cpm_max": 0.0,           # 0 = this rule disabled
    "rule_cpc_max": 0.0,
    "rule_cpa_max": 0.0,
    "rule_min_spend": 10.0,        # $ a campaign must spend before rules judge it
    # --- profit-based rules (P&L truth from postbacks) ------------------------
    "profit_rules_enabled": False,
    "profit_loss_limit": 20.0,     # pause a source losing more than this today
    "profit_min_spend": 15.0,      # only judge sources past this spend today
    "protect_profitable": True,    # metric rules skip sources in profit today
    # --- auto top-ups ---------------------------------------------------------
    "topup_enabled": False,
    "topup_below": 50.0,           # trigger: account balance below this
    "topup_amount": 100.0,         # transfer this much from the BC wallet
    "topup_daily_cap": 300.0,      # max transferred per account per local day
    # --- sweeps ---------------------------------------------------------------
    "sweep_interval_sec": 60,      # fast loop: active-campaign metrics + rules
    "slow_every_n_sweeps": 5,      # balances/top-ups/full sync every Nth sweep
    # --- account lifecycle -----------------------------------------------------
    "account_error_threshold": 3,  # consecutive launch failures before cooldown
    "cooldown_hours": 48,
    "min_fresh_accounts": 5,       # alert when fresh (never-launched) inventory dips below
    # --- launch queue ----------------------------------------------------------
    "queue_per_sweep": 3,          # launches processed per background sweep
    "launch_retry_max": 3,         # attempts for transient TikTok errors
    # --- sources / postback ---------------------------------------------------
    "url_param": "source",         # query param appended to the landing URL
    "postback_key": "",            # generated on first read; auths /postback
    "postback_mode": "incremental",  # incremental = sum every postback;
                                     # snapshot = latest value per source per day
    # --- creative library ------------------------------------------------------
    "ad_identity_name": "",        # display name for the CUSTOMIZED_USER identity
                                   # shown on uploaded-creative ads (avatar file is
                                   # managed on the /creatives page)
}


def get_settings(db: Session) -> dict:
    row = db.query(models.Setting).filter_by(key=KEY).first()
    data = {}
    if row and row.value:
        try:
            data = json.loads(row.value)
        except json.JSONDecodeError:
            data = {}
    merged = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    if not merged["postback_key"]:
        merged["postback_key"] = secrets.token_hex(16)
        save_settings(db, merged)
    return merged


def save_settings(db: Session, values: dict):
    clean = {}
    for k, default in DEFAULTS.items():
        v = values.get(k, default)
        try:
            if isinstance(default, bool):
                clean[k] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "on", "yes")
            elif isinstance(default, float):
                clean[k] = max(float(v or 0), 0.0)
            elif isinstance(default, int):
                clean[k] = max(int(float(v or 0)), 0)
            else:
                clean[k] = str(v or "").strip()
        except (TypeError, ValueError):
            clean[k] = default
    # sanity floors
    clean["sweep_interval_sec"] = max(clean["sweep_interval_sec"], 30)
    clean["slow_every_n_sweeps"] = max(clean["slow_every_n_sweeps"], 1)
    if clean["url_param"] == "":
        clean["url_param"] = "source"
    row = db.query(models.Setting).filter_by(key=KEY).first()
    if not row:
        row = models.Setting(key=KEY)
        db.add(row)
    row.value = json.dumps(clean)
    db.commit()
