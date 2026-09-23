"""Runtime-tunable settings, stored in the Setting KV table so the operator
can change them on the /settings page without redeploying. The background
worker re-reads them every sweep, so changes apply within a minute.

Two layers (v119, per-user workspaces):
  * USER_KEYS  — each user's own: rules, top-ups, account lifecycle, tracking,
                 postback key/mode, Events API, appeals. Stored under
                 "app_settings:u<user id>". A user's launches, postbacks and rules
                 read THEIR row; the super admin viewing a user sees that user's.
  * GLOBAL_KEYS — one set for the server (sweep timing, launch queue pacing,
                 audience refresh, assistant model), owner-only, in "app_settings".
The pre-workspaces values (one global row) are migrated into the super admin's
row on first read, so the postback key Glitchy already has keeps working."""
from __future__ import annotations

import re

import json
import secrets

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import models

KEY = "app_settings"
USER_PREFIX = KEY + ":u"        # + user id

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
    # --- rule rails (v148) --------------------------------------------------------
    "rules_mode": "pause",         # pause = act · flag = tell me, don't touch · dry_run = log what it WOULD do, silently
    "rules_hourly_cap": 10,        # at most this many rule pauses per hour (the rest are held and reported)
    "profit_lookback": "today",    # the window profit rules judge: today | yesterday | 3d | 7d
    "profit_roas_min": 0.0,        # also pause a source whose ROAS over that window is below this (0 = off)
    # --- idle bid bump ---------------------------------------------------------
    "bid_bump_enabled": False,     # raise the bid of a delivering ad group that hasn't spent for a while
    "bid_bump_step": 0.05,         # $ added per bump
    "bid_bump_idle_min": 60,       # minutes without any spend before a bump
    "bid_bump_ceiling": 0.0,       # never bid above this ($); 0 = no ceiling
    "bid_bump_max_per_day": 6,     # bumps per ad group per local day (hard stop)
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
    "clickflare_agid_field": 6,    # …and the TikTok ad group ID (6 = adset_id in the same template) — revenue per ad group
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
    "events_value_mode": "payout",   # payout = the postback's revenue · fixed = always events_value_fixed (v125)
    "events_value_fixed": 0.0,       # the fixed value per event (e.g. 6.00) in fixed mode
    "events_value_match": "",        # fixed mode only for sources / landing URLs containing one of these (comma list); empty = every event
    "lpv_enabled": False,            # v127: fire a server-side event for every lander VIEW (see lpv_events.py)
    "lpv_event": "CompleteRegistration",
    "lpv_value": 6.0,
    "lpv_pages": "play",             # lander page names / slugs it applies to (comma list); empty = every page
    "events_test_code": "",        # TikTok test_event_code (Events Manager test tab)
    "events_page_url": "",         # page.url sent with events (REQUIRED for web events) when the
                                   #   source has no launch to take the landing URL from

    # --- automatic ad-rejection appeals (/adgroup/appeal/) -----------------
    "appeal_auto_enabled": False,  # file an appeal for every newly rejected ad found by the scan
    "appeal_reason": DEFAULT_APPEAL_REASON,  # text sent with each appeal; {ad_name} {campaign_name} {reasons} fill in
    "appeal_skip_keywords": "",    # comma-separated; a rejection whose TikTok reason contains one is NOT auto-appealed
    "appeal_daily_cap": 50,        # max auto-appeals per local day (an account-wide problem must not burn every appeal)
    "issue_max_age_days": 3,       # rejected-ad issues whose ad was last changed more than this many days ago are
                                   #   dropped from Health/Inbox so old rejections don't stack up forever (0 = keep all)

    # --- alerts pushed out (notify.py, v149) ------------------------------------
    "notify_telegram_token": "",   # bot token from @BotFather — sealed at rest
    "notify_telegram_chat": "",    # the chat / group id the bot posts to
    "notify_email_to": "",         # needs SMTP_* on the server
    "notify_level": "err",         # err = errors only · warn = errors + warnings

    # --- audience page refresh ------------------------------------------------
    "audience_hours_every_min": 10,      # today's hour-by-hour delivery (basic report, near real-time): accounts with active campaigns
    "audience_breakdown_every_min": 60,  # today+yesterday audience breakdowns (TikTok publishes them 10–12 h late): active accounts
    # --- assistant ------------------------------------------------------------
    "assistant_model": "claude-sonnet-5",   # model id used by the Assistant page (API key comes from the ANTHROPIC_API_KEY env var)
}
AUDIENCE_HOURS_MIN = 5          # floor: one report call per active account per run
AUDIENCE_BREAKDOWN_MIN = 15     # floor: ~8 calls per active account per run


GLOBAL_KEYS = frozenset({
    "sweep_interval_sec", "slow_every_n_sweeps", "queue_per_sweep", "launch_retry_max", "launch_pace_sec",
    "audience_hours_every_min", "audience_breakdown_every_min", "assistant_model", "issue_max_age_days",
})
USER_KEYS = frozenset(k for k in DEFAULTS if k not in GLOBAL_KEYS)


def _sealed(d: dict) -> dict:
    from . import secrets_box
    return secrets_box.settings_seal(d)


def user_key(user_id: int) -> str:
    return f"{USER_PREFIX}{int(user_id)}"


def _load(db: Session, key: str):
    """The stored dict for one row, or None when the row doesn't exist."""
    row = db.query(models.Setting).filter_by(key=key).first()
    if row is None:
        return None
    try:
        data = json.loads(row.value) if row.value else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        return {}
    from . import secrets_box
    return secrets_box.settings_unseal(data)          # the Events API token is sealed at rest


def owner_id(db: Session):
    """The super admin's user id (OWNER_EMAIL), or None before that account exists."""
    from . import scope
    u = scope.super_admin(db)
    return u.id if u is not None else None


def _mint_user_row(db: Session, user_id: int, base: dict) -> dict:
    """First read for a user: their row starts from `base` (the pre-workspaces values
    for the owner, the defaults for everyone else) with a fresh postback key unless
    `base` already carries one. Several simultaneous first readers each try — exactly
    one INSERT lands and everyone re-reads that one."""
    from . import queries
    data = {k: base.get(k, DEFAULTS[k]) for k in USER_KEYS}
    if not data.get("postback_key"):
        data["postback_key"] = secrets.token_hex(16)
    queries.insert_setting_if_absent(db, user_key(user_id), json.dumps(_sealed(data)))
    db.commit()
    return _load(db, user_key(user_id)) or data


def get_settings(db: Session, user_id: int | None = None) -> dict:
    """Settings as one flat dict: the server's GLOBAL_KEYS plus one user's USER_KEYS.
    `user_id` None = the super admin's (the company defaults, and what background
    code without a user in hand should use for unowned accounts)."""
    gdata = _load(db, KEY) or {}
    merged = {**DEFAULTS, **{k: v for k, v in gdata.items() if k in GLOBAL_KEYS}}
    if user_id is None:
        user_id = owner_id(db)
    if user_id is None:
        # no owner account yet (first boot, tests): the legacy single row is the truth
        merged.update({k: v for k, v in gdata.items() if k in USER_KEYS})
        if not merged["postback_key"]:
            merged["postback_key"] = secrets.token_hex(16)
            from . import queries
            from .database import safe_commit
            queries.upsert_setting(db, KEY, json.dumps(_sealed({**gdata, "postback_key": merged["postback_key"]})))
            # if the one writer is busy, don't 500 the page: return the key in memory,
            # it persists on the next call that gets the lock
            safe_commit(db)
        return merged
    udata = _load(db, user_key(user_id))
    if udata is None:
        # the owner inherits the pre-workspaces row (same postback key, same tracking
        # setup); anyone else starts from the defaults with their own key
        base = {k: v for k, v in gdata.items() if k in USER_KEYS} if user_id == owner_id(db) else {}
        udata = _mint_user_row(db, user_id, base)
    merged.update({k: v for k, v in udata.items() if k in USER_KEYS})
    if not merged["postback_key"]:
        merged["postback_key"] = secrets.token_hex(16)
        try:
            save_settings(db, merged, user_id=user_id, global_too=False)
        except OperationalError as exc:  # one busy writer must not 500 a page load
            if "lock" not in str(getattr(exc, "orig", exc)).lower() and "busy" not in str(getattr(exc, "orig", exc)).lower():
                raise
            db.rollback()   # key stays in memory this request; persists on the next
    return merged


def for_view(db: Session) -> dict:
    """The settings of the workspace the current request is looking at (the user
    themselves; for the super admin, the user they switched to — or their own)."""
    from . import ctx
    return get_settings(db, ctx.OWNER.get())


def for_account(db: Session, advertiser_id: str, cache: dict | None = None) -> dict:
    """The settings of the user who owns an ad account (unowned → the owner's).
    `cache` (owner id → settings) keeps a loop over many accounts to one read per user."""
    uid = db.query(models.AdAccount.owner_user_id).filter_by(advertiser_id=str(advertiser_id or "")).scalar()
    return _cached(db, uid, cache)


def _cached(db: Session, uid, cache: dict | None) -> dict:
    if cache is None:
        return get_settings(db, uid)
    key = uid if uid is not None else "owner"
    if key not in cache:
        cache[key] = get_settings(db, uid)
    return cache[key]


def per_user(db: Session) -> list[tuple]:
    """[(user, settings, owned advertiser ids)] for every active user — the background
    loop runs each user's rules over their own accounts with their own thresholds.
    Accounts nobody owns count as the super admin's."""
    from . import scope
    oid = owner_id(db)
    owned: dict = {}
    for aid, uid in db.query(models.AdAccount.advertiser_id, models.AdAccount.owner_user_id):
        owned.setdefault(uid if uid is not None else oid, set()).add(aid)
    out = []
    for u in db.query(models.User).filter(models.User.active == True).order_by(models.User.id):  # noqa: E712
        out.append((u, get_settings(db, u.id), owned.get(u.id, set())))
    return out


def user_for_postback_key(db: Session, key: str):
    """Which user's postback key this is (their row, or the legacy global row → the
    owner). None when it matches nobody. Constant-time compares."""
    key = str(key or "")
    if not key:
        return None
    for row in db.query(models.Setting).filter(models.Setting.key.like(USER_PREFIX + "%")).all():
        try:
            k = str((json.loads(row.value or "{}") or {}).get("postback_key") or "")
        except (json.JSONDecodeError, AttributeError):
            continue
        if k and secrets.compare_digest(str(k).encode(), str(key).encode()):
            try:
                return int(row.key[len(USER_PREFIX):])
            except ValueError:
                continue
    gdata = _load(db, KEY) or {}
    gk = str(gdata.get("postback_key") or "")
    if gk and secrets.compare_digest(str(gk).encode(), str(key).encode()):
        oid = owner_id(db)
        if oid is not None:
            get_settings(db, oid)           # migrates the legacy row into the owner's on first use
        return oid if oid is not None else -1   # -1 = legacy single-row install (no owner account yet)
    return None


def save_settings(db: Session, values: dict, user_id: int | None = None, global_too: bool = True):
    """Write one user's USER_KEYS (`user_id` None = the super admin's row) and, when
    `global_too`, the server's GLOBAL_KEYS. A buyer's save never touches the global
    row; an empty postback key never overwrites the stored one."""
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
    if clean.get("rules_mode") not in ("pause", "flag", "dry_run"):
        clean["rules_mode"] = "pause"
    if clean.get("profit_lookback") not in ("today", "yesterday", "3d", "7d"):
        clean["profit_lookback"] = "today"
    if clean.get("source_mode") not in ("campaign", "static"):
        clean["source_mode"] = "campaign"
    if clean.get("tracking_mode") not in ("direct", "redirect", "clickflare"):
        clean["tracking_mode"] = "direct"
    clean["clickflare_field"] = min(max(int(clean.get("clickflare_field") or 3), 1), 20)
    _cidf = clean.get("clickflare_cid_field")
    clean["clickflare_cid_field"] = min(max(int(4 if _cidf in (None, "") else _cidf), 0), 20)   # 0 = don't send
    _agf = clean.get("clickflare_agid_field")
    clean["clickflare_agid_field"] = min(max(int(6 if _agf in (None, "") else _agf), 0), 20)    # 0 = don't send
    clean["tracking_domain"] = str(clean.get("tracking_domain") or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    # a TikTok test_event_code is a short token from Events Manager → Test events (e.g.
    # TEST1234). Anything else (an email, a sentence) would mark EVERY forwarded event as
    # a test event, which TikTok never counts for optimisation — so it is dropped here.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(clean.get("events_test_code") or "")):
        clean["events_test_code"] = ""
    if clean.get("events_event_mode") not in ("campaign", "fixed"):
        clean["events_event_mode"] = "campaign"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,40}", str(clean.get("lpv_event") or "")):
        clean["lpv_event"] = "CompleteRegistration"
    clean["lpv_pages"] = ",".join(w.strip().lower() for w in str(clean.get("lpv_pages") or "").split(",") if w.strip())[:400]
    if clean.get("events_value_mode") not in ("payout", "fixed"):
        clean["events_value_mode"] = "payout"
    clean["events_value_match"] = ",".join(w.strip().lower() for w in str(clean.get("events_value_match") or "").split(",") if w.strip())[:400]
    if not clean.get("appeal_reason"):
        clean["appeal_reason"] = DEFAULT_APPEAL_REASON
    clean["appeal_reason"] = clean["appeal_reason"][:APPEAL_REASON_MAX]
    clean["appeal_daily_cap"] = max(int(clean.get("appeal_daily_cap") or 0), 1)
    clean["bid_bump_step"] = round(min(max(float(clean.get("bid_bump_step") or 0), 0.0), 100.0), 2)
    clean["bid_bump_idle_min"] = max(int(clean.get("bid_bump_idle_min") or 0), 15)      # the sweep sees spend once a minute; below 15 min is noise
    clean["bid_bump_max_per_day"] = min(max(int(clean.get("bid_bump_max_per_day") or 0), 1), 48)
    clean["audience_hours_every_min"] = max(int(clean.get("audience_hours_every_min") or 0), AUDIENCE_HOURS_MIN)
    clean["audience_breakdown_every_min"] = max(int(clean.get("audience_breakdown_every_min") or 0), AUDIENCE_BREAKDOWN_MIN)
    from . import queries
    if user_id is None:
        user_id = owner_id(db)
    if user_id is None:
        # no owner account yet: the legacy single row holds everything
        queries.upsert_setting(db, KEY, json.dumps(_sealed(clean)))      # atomic: two first-time readers can't both INSERT
        db.commit()
        return
    if global_too:
        gdata = _load(db, KEY) or {}
        gdata.update({k: clean[k] for k in GLOBAL_KEYS})
        queries.upsert_setting(db, KEY, json.dumps(_sealed(gdata)))
    udata = _load(db, user_key(user_id)) or {}
    if not clean.get("postback_key"):
        clean["postback_key"] = udata.get("postback_key") or secrets.token_hex(16)
    udata.update({k: clean[k] for k in USER_KEYS})
    queries.upsert_setting(db, user_key(user_id), json.dumps(_sealed(udata)))
    db.commit()
