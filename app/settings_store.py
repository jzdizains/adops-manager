"""Runtime-tunable settings, stored in the Setting KV table so the operator
can change them on the /settings page without redeploying. The background
worker re-reads them every sweep, so changes apply within a minute."""
from __future__ import annotations

import re

import json
import secrets

from sqlalchemy.orm import Session

from . import models

KEY = "app_settings"

# Sent with every automatic appeal (the Ads Manager form makes a description
# mandatory; the API field is appeal_reason). Kept factual and generic — an
# appeal is a request for a second human look, not an argument.
DEFAULT_APPEAL_REASON = (
    "Requesting a second review of this ad. The creative, text and landing page follow "
    "TikTok's advertising policies for this category; we believe the rejection "
    "({reasons}) was applied in error. Please re-evaluate.")
APPEAL_REASON_MAX = 500   # the API models cap appeal text at 512 chars; leave headroom for placeholders

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
    "launch_pace_sec": 1.0,        # pause between accounts in a direct batch (rate-limit safety)
    # --- sources / postback ---------------------------------------------------
    "url_param": "source",         # query param appended to the landing URL
    "url_param_extra": "",         # extra names the OFFER link should also carry the source under (Glitchy sub ids)
    "source_mode": "campaign",     # campaign = ?source=__CAMPAIGN_NAME__ (TikTok fills the campaign
                                   #   name at click time; names made URL-safe + unique)
                                   # static   = per spark/creative source (legacy)
    "postback_key": "",            # generated on first read; auths /postback
    "postback_mode": "incremental",  # incremental = sum every postback;
                                     # snapshot = latest value per source per day
    # --- TikTok Events API (S2S postback → pixel) ------------------------------
    "tracking_mode": "direct",     # direct = script on the lander registers the click (no redirect)
                                   # redirect = the ad points at /t/c which records the click, then redirects
                                   # clickflare = ClickFlare is the tracker; it postbacks every conversion here
    "tracking_domain": "",         # public host the landers/ads reach this app on (empty = POSTBACK_HOST or this site)
    "clickflare_field": 3,         # ClickFlare tracking field that holds the TikTok campaign name (3 in ClickFlare's TikTok template)
    "clickflare_cid_field": 4,     # …and the TikTok campaign ID (4 in the same template) — the fallback when the name is missing/renamed
    "events_api_enabled": False,   # forward postbacks with a ttclid to TikTok
    "events_pixel_code": "",       # pixel ID to fire to; empty = auto-resolve
                                   # from the source's launch (PixelCache)
    "events_access_token": "",     # dedicated Events API token (Events Manager →
                                   # pixel → Settings → Generate Access Token);
                                   # empty = try the account's Marketing token
    "events_event_mode": "campaign",  # campaign = fire the event the campaign's ad group optimises for
                                      #   (split tests: CompleteRegistration vs CompletePayment side by side)
                                      # fixed    = always fire events_event_name
    "events_event_name": "CompleteRegistration",  # TikTok standard web event to fire (fixed mode / fallback)
    "events_currency": "USD",
    "events_test_code": "",        # TikTok test_event_code (Events Manager test tab)
    "events_page_url": "",         # page.url sent with events (REQUIRED for web events) when the
                                   #   source has no launch to take the landing URL from

    # --- automatic ad-rejection appeals (/adgroup/appeal/) -----------------
    "appeal_auto_enabled": False,  # file an appeal for every newly rejected ad found by the scan
    "appeal_reason": DEFAULT_APPEAL_REASON,  # text sent with each appeal; {ad_name} {campaign_name} {reasons} fill in
    "appeal_skip_keywords": "",    # comma-separated; a rejection whose TikTok reason contains one is NOT auto-appealed
    "appeal_daily_cap": 50,        # max auto-appeals per local day (an account-wide problem must not burn every appeal)

    # --- audience page refresh ------------------------------------------------
    "audience_hours_every_min": 10,      # today's hour-by-hour delivery (basic report, near real-time): accounts with active campaigns
    "audience_breakdown_every_min": 60,  # today+yesterday audience breakdowns (TikTok publishes them 10–12 h late): active accounts
    # --- assistant ------------------------------------------------------------
    "assistant_model": "claude-sonnet-5",   # model id used by the Assistant page (API key comes from the ANTHROPIC_API_KEY env var)
}
AUDIENCE_HOURS_MIN = 5          # floor: one report call per active account per run
AUDIENCE_BREAKDOWN_MIN = 15     # floor: ~8 calls per active account per run


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
        # first ever read: mint the postback key. Several simultaneous first readers each try —
        # only ONE insert lands (ON CONFLICT DO NOTHING) and everyone re-reads that key.
        merged["postback_key"] = secrets.token_hex(16)
        if row is None:
            from . import queries
            queries.insert_setting_if_absent(db, KEY, json.dumps(merged))
            db.commit()
        else:
            save_settings(db, merged)
        db.expire_all()
        row = db.query(models.Setting).filter_by(key=KEY).first()
        try:
            data = json.loads(row.value) if row and row.value else {}
        except json.JSONDecodeError:
            data = {}
        merged = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
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
    clean["url_param_extra"] = ",".join(
        w for w in re.findall(r"[A-Za-z0-9_]+", str(clean.get("url_param_extra") or ""))[:4])
    if clean.get("source_mode") not in ("campaign", "static"):
        clean["source_mode"] = "campaign"
    if clean.get("tracking_mode") not in ("direct", "redirect", "clickflare"):
        clean["tracking_mode"] = "direct"
    clean["clickflare_field"] = min(max(int(clean.get("clickflare_field") or 3), 1), 20)
    _cidf = clean.get("clickflare_cid_field")
    clean["clickflare_cid_field"] = min(max(int(4 if _cidf in (None, "") else _cidf), 0), 20)   # 0 = don't send
    clean["tracking_domain"] = str(clean.get("tracking_domain") or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    # a TikTok test_event_code is a short token from Events Manager → Test events (e.g.
    # TEST1234). Anything else (an email, a sentence) would mark EVERY forwarded event as
    # a test event, which TikTok never counts for optimisation — so it is dropped here.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(clean.get("events_test_code") or "")):
        clean["events_test_code"] = ""
    if clean.get("events_event_mode") not in ("campaign", "fixed"):
        clean["events_event_mode"] = "campaign"
    if not clean.get("appeal_reason"):
        clean["appeal_reason"] = DEFAULT_APPEAL_REASON
    clean["appeal_reason"] = clean["appeal_reason"][:APPEAL_REASON_MAX]
    clean["appeal_daily_cap"] = max(int(clean.get("appeal_daily_cap") or 0), 1)
    clean["audience_hours_every_min"] = max(int(clean.get("audience_hours_every_min") or 0), AUDIENCE_HOURS_MIN)
    clean["audience_breakdown_every_min"] = max(int(clean.get("audience_breakdown_every_min") or 0), AUDIENCE_BREAKDOWN_MIN)
    from . import queries
    queries.upsert_setting(db, KEY, json.dumps(clean))      # atomic: two first-time readers can't both INSERT
    db.commit()
