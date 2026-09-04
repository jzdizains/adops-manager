"""SQLAlchemy models — the ~17 tables the launch engine depends on.

Faithful to the clone guide §4. Notable rules:
  * Template: CBO fields (`campaign_budget_mode`, `campaign_budget`) are
    top-level COLUMNS, never inside the `adgroup_settings` JSON blob (§9.1).
  * TimeSession: the hourly rate is STAMPED at clock-in so later rate edits
    never rewrite historical pay.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import relationship

from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Accounts & auth
# ---------------------------------------------------------------------------

class BusinessCenter(Base):
    """One row per Business Center under the connected TikTok login.

    The wallet `balance` drives the low-balance alert ($50 threshold by
    default, overridable per BC via `alert_threshold`)."""
    __tablename__ = "business_centers"

    id = Column(Integer, primary_key=True)
    bc_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, default="")
    balance = Column(Float, default=0.0)
    currency = Column(String, default="USD")
    status = Column(String, default="")
    alert_threshold = Column(Float, default=50.0)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Alert(Base):
    """In-app alerts (bell icon + Overview banner). One row per event;
    `acknowledged` hides it; while a condition persists a fresh reminder row
    is created at most once every 24h."""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False)          # bc_low_balance | account_error | rule_action | info
    ref_id = Column(String, default="", index=True)  # e.g. the bc_id or advertiser_id concerned
    level = Column(String, default="warn")         # info | warn | err
    message = Column(Text, default="")
    acknowledged = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow, index=True)


class AdAccount(Base):
    __tablename__ = "ad_accounts"

    id = Column(Integer, primary_key=True)
    advertiser_id = Column(String, unique=True, index=True, nullable=False)
    advertiser_name = Column(String, default="")
    access_token = Column(Text, default="")          # shared across advertisers
    refresh_token = Column(Text, default="")
    token_expires_at = Column(DateTime, nullable=True)
    refresh_expires_at = Column(DateTime, nullable=True)
    status = Column(String, default="")              # TikTok advertiser status
    owner_bc_id = Column(String, default="")
    currency = Column(String, default="USD")
    timezone = Column(String, default="")
    region_codes = Column(Text, default="")          # cached /tool/region result (JSON)
    balance = Column(Float, default=0.0)
    enabled = Column(Boolean, default=True)          # operator can hide accounts
    error_count = Column(Integer, default=0)         # consecutive launch failures
    cooldown_until = Column(DateTime, nullable=True) # lifecycle: excluded from auto-pick until then
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Presets ("templates")
# ---------------------------------------------------------------------------

class Template(Base):
    __tablename__ = "templates"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    # -- top-level campaign fields as COLUMNS (⚠ §9.1: CBO lives HERE) --------
    objective_type = Column(String, default="TRAFFIC")     # TRAFFIC | WEB_CONVERSIONS | LEAD_GENERATION | ...
    campaign_budget_mode = Column(String, default="ABO")   # ABO | BUDGET_MODE_DAY (CBO daily) | BUDGET_MODE_TOTAL
    campaign_budget = Column(Float, nullable=True)         # CBO amount (None for ABO)
    campaign_name_pattern = Column(String, default="")     # optional naming pattern
    # -- everything ad-group/creative/spark/pixel lives in the JSON blob ------
    adgroup_settings = Column(Text, default="{}")          # JSON: see templates_routes.parse_form
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Spark codes
# ---------------------------------------------------------------------------

class Creative(Base):
    """Uploaded video creative (Creative Library). Each creative is consumed by
    exactly ONE launch — the engine reserves it, uploads it into the target
    account's asset library, and marks where it went."""
    __tablename__ = "creatives"

    id = Column(Integer, primary_key=True)
    name = Column(String, default="")                      # display label (defaults to file name)
    file_name = Column(String, default="")                 # original upload name
    file_path = Column(String, default="")                 # under DATA_DIR/creatives/
    md5 = Column(String, default="", index=True)
    size_bytes = Column(Integer, default=0)
    source = Column(String, default="", index=True)        # P&L join key (like spark source)
    status = Column(String, default="available")           # available | used | processing | error
    kind = Column(String, default="video")                 # video | image (images never enter the video launch pool)
    used_advertiser_id = Column(String, default="")
    used_campaign_id = Column(String, default="")
    used_at = Column(DateTime, nullable=True)
    uploaded_at = Column(DateTime, default=utcnow)
    # --- carousel (kind="carousel"): ordered slides + the mandatory soundtrack --
    carousel_images = Column(Text, default="")             # JSON list of image Creative ids, in order (first = cover)
    music_id = Column(String, default="")                  # TikTok music_id (Commercial Music Library / uploaded)
    music_name = Column(String, default="")
    music_author = Column(String, default="")
    # --- AI image editing (Gemini / "Nano Banana") ----------------------------
    ai_prompt = Column(Text, default="")                   # prompt that produced this image
    ai_model = Column(String, default="")                  # gemini model id used
    ai_cost = Column(Float, default=0.0)                   # list price per generated image (USD)
    # --- variation processing (TensorPix): each reads as a new video ----------
    freshen = Column(Boolean, default=False)               # is this a processed variant?
    freshen_intensity = Column(String, default="")         # uniquify strength: light|medium|strong
    freshen_mirror = Column(Boolean, default=False)        # (unused)
    uniquify = Column(Boolean, default=False)              # apply slowdown+colour+audio pass
    src_path = Column(String, default="")                  # source awaiting processing
    source_md5 = Column(String, default="", index=True)    # md5 of the ORIGINAL upload
    error = Column(Text, default="")                       # processing failure detail
    tp_model_ids = Column(String, default="")              # selected TensorPix model ids (csv)
    tp_video_id = Column(String, default="")               # TensorPix uploaded-source id
    tp_job_id = Column(String, default="")                 # TensorPix job id for this variant
    tp_cost = Column(Float, default=0.0)                   # job cost (USD) from TensorPix
    tp_checked_at = Column(DateTime, nullable=True)        # poll throttle


class CreativeUpload(Base):
    """Cache: which TikTok video_id/cover a creative got in a given ad account
    (so a retried launch never re-uploads the file)."""
    __tablename__ = "creative_uploads"

    id = Column(Integer, primary_key=True)
    creative_id = Column(Integer, ForeignKey("creatives.id"), index=True)
    advertiser_id = Column(String, index=True)
    video_id = Column(String, default="")
    cover_image_id = Column(String, default="")
    image_id = Column(String, default="")                  # image creatives: the uploaded image id
    image_url = Column(Text, default="")                   # …and TikTok-hosted URL (music recommendations)
    upload_md5 = Column(String, default="")                # md5 of the file ACTUALLY sent (delivery copy);
    #   a cached image row whose md5 differs — or is empty (uploaded before the
    #   carousel-size delivery copies existed) — is stale and gets re-uploaded
    created_at = Column(DateTime, default=utcnow)


class AdText(Base):
    """Ad-text pool — each text is consumed by exactly ONE launch so every
    campaign can carry unique copy."""
    __tablename__ = "ad_texts"

    id = Column(Integer, primary_key=True)
    text = Column(Text, default="")
    status = Column(String, default="available")           # available | used
    used_advertiser_id = Column(String, default="")
    used_campaign_id = Column(String, default="")
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class SparkCodeGroup(Base):
    __tablename__ = "spark_code_groups"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)                  # usually the creator handle
    created_at = Column(DateTime, default=utcnow)

    codes = relationship("SparkCode", back_populates="group")


class SparkCode(Base):
    __tablename__ = "spark_codes"

    id = Column(Integer, primary_key=True)
    name = Column(String, default="")
    code = Column(Text, nullable=False)                    # pasted auth code OR item_info.auth_code
    source = Column(String, default="", index=True)        # operator-entered source (P&L join key)
    media_type = Column(String, default="VIDEO")           # VIDEO | CAROUSEL
    tiktok_post_url = Column(String, default="")
    thumbnail_url = Column(Text, default="")
    tiktok_item_id = Column(String, default="")            # set for auto-grabbed sparks; null/"" for hand-entered
    group_id = Column(Integer, ForeignKey("spark_code_groups.id"), nullable=True)
    status = Column(String, default="active")              # active | used | expired
    use_count = Column(Integer, default=0)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    group = relationship("SparkCodeGroup", back_populates="codes")


class SparkSetting(Base):
    """'Which creators are mine' filter for the spark hub."""
    __tablename__ = "spark_settings"

    id = Column(Integer, primary_key=True)
    creator_handle = Column(String, unique=True, nullable=False)
    is_mine = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# TikTok per-account assets
# ---------------------------------------------------------------------------

class InstantPage(Base):
    __tablename__ = "instant_pages"

    id = Column(Integer, primary_key=True)
    page_id = Column(String, nullable=False)
    name = Column(String, default="")
    owner_advertiser_id = Column(String, nullable=False)   # per-account asset — cloning uses web path
    preview_url = Column(Text, default="")
    status = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)


class LeadForm(Base):
    __tablename__ = "lead_forms"

    id = Column(Integer, primary_key=True)
    form_id = Column(String, nullable=False)
    name = Column(String, default="")
    owner_advertiser_id = Column(String, nullable=False)
    status = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)


class PixelCache(Base):
    """pixel CODE -> numeric pixel_id resolution cache (§9.7)."""
    __tablename__ = "pixel_cache"

    id = Column(Integer, primary_key=True)
    advertiser_id = Column(String, nullable=False, index=True)
    pixel_code = Column(String, nullable=False)
    pixel_id = Column(String, nullable=False)
    pixel_name = Column(String, default="")
    resolved_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Revenue / conversions
# ---------------------------------------------------------------------------

class ConversionSample(Base):
    """Persisted revenue/conversion samples so dashboards never make slow live calls."""
    __tablename__ = "conversion_samples"

    id = Column(Integer, primary_key=True)
    network = Column(String, nullable=False)               # legacy network samples (unused)
    sub_id = Column(String, default="")                    # maps back to campaign/account
    conversions = Column(Integer, default=0)
    revenue = Column(Float, default=0.0)
    sampled_at = Column(DateTime, default=utcnow, index=True)
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Launch + campaign bookkeeping
# ---------------------------------------------------------------------------

class LaunchLog(Base):
    """One row per account per launch attempt — the launch-result page reads these."""
    __tablename__ = "launch_logs"

    id = Column(Integer, primary_key=True)
    batch_ref = Column(String, index=True)                 # short ref id shown to the operator
    advertiser_id = Column(String, nullable=False)
    advertiser_name = Column(String, default="")
    template_id = Column(Integer, nullable=True)
    template_name = Column(String, default="")
    campaign_id = Column(String, default="")
    spark_code_id = Column(Integer, nullable=True)
    source = Column(String, default="", index=True)        # source active on this launch
    landing_url = Column(Text, default="")                 # the exact landing URL sent to TikTok (carries ?source=)
    optimization_event = Column(String, default="")        # pixel event the ad group optimises for (ON_WEB_REGISTER, SHOPPING…)
    ok = Column(Boolean, default=False)
    error_code = Column(String, default="")
    error_message = Column(Text, default="")               # plain-English
    error_technical = Column(Text, default="")             # raw TikTok detail (copyable)
    created_at = Column(DateTime, default=utcnow, index=True)


class CampaignRecord(Base):
    """Synced campaign cache for the Status page ('synced X ago')."""
    __tablename__ = "campaign_records"

    id = Column(Integer, primary_key=True)
    advertiser_id = Column(String, index=True, nullable=False)
    campaign_id = Column(String, index=True, nullable=False)
    campaign_name = Column(String, default="")
    objective_type = Column(String, default="")
    operation_status = Column(String, default="")          # ENABLE | DISABLE
    secondary_status = Column(String, default="")
    budget = Column(Float, default=0.0)
    budget_mode = Column(String, default="")
    spend_today = Column(Float, default=0.0)
    # -- metric cache for the Monitor page (today's numbers) ------------------
    impressions = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    conversions = Column(Integer, default=0)
    cpm = Column(Float, default=0.0)
    cpc = Column(Float, default=0.0)
    cpa = Column(Float, default=0.0)
    ctr = Column(Float, default=0.0)
    launched_at = Column(DateTime, nullable=True)  # campaign create_time from TikTok
    is_smart_plus = Column(Boolean, default=False)  # Smart+ campaigns use /smart_plus/* endpoints
    synced_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Sources / postback / P&L
# ---------------------------------------------------------------------------

class PostbackEvent(Base):
    """One row per postback hit from Glitchy (revenue truth for the P&L)."""
    __tablename__ = "postback_events"

    id = Column(Integer, primary_key=True)
    source = Column(String, default="", index=True)
    revenue = Column(Float, default=0.0)
    clicks = Column(Integer, default=0)
    conversions = Column(Integer, default=0)
    cvr = Column(Float, default=0.0)
    txn = Column(String, default="", index=True)       # transaction id (dedupe key)
    ttclid = Column(String, default="")                # TikTok click id (Events API)
    event = Column(String, default="")                 # e.g. purchase
    forward_status = Column(String, default="")        # Events API: sent | skipped… | error…
    raw_query = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow, index=True)


class SpendSnapshot(Base):
    """Per-campaign per-local-day spend, upserted each sweep — builds spend
    history so the P&L can cover any date range going forward."""
    __tablename__ = "spend_snapshots"

    id = Column(Integer, primary_key=True)
    advertiser_id = Column(String, index=True, nullable=False)
    campaign_id = Column(String, index=True, nullable=False)
    day = Column(String, index=True, nullable=False)       # YYYY-MM-DD in business TZ
    spend = Column(Float, default=0.0)
    conversions = Column(Integer, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Automation logs
# ---------------------------------------------------------------------------

class RuleAction(Base):
    """Every action the rule engine took (audit trail)."""
    __tablename__ = "rule_actions"

    id = Column(Integer, primary_key=True)
    advertiser_id = Column(String, default="")
    campaign_id = Column(String, index=True, default="")
    campaign_name = Column(String, default="")
    rule = Column(String, default="")                      # e.g. "cpa > 30.00"
    metric_value = Column(Float, default=0.0)
    action = Column(String, default="pause")
    ok = Column(Boolean, default=True)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow, index=True)


class TopUp(Base):
    """Every auto top-up transfer attempted via /bc/transfer/."""
    __tablename__ = "topups"

    id = Column(Integer, primary_key=True)
    bc_id = Column(String, default="")
    advertiser_id = Column(String, index=True, default="")
    amount = Column(Float, default=0.0)
    day = Column(String, index=True, default="")           # YYYY-MM-DD business TZ (caps)
    ok = Column(Boolean, default=True)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow, index=True)


class LaunchQueueItem(Base):
    """Queued launch: processed by the background worker, retried on transient
    TikTok errors. advertiser_id empty = auto-pick an account at process time."""
    __tablename__ = "launch_queue"

    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, nullable=False)
    spark_code_id = Column(Integer, nullable=True)     # optional spark override
    use_library = Column(Boolean, default=False)       # override: pull library creatives
    advertiser_id = Column(String, default="")         # "" = auto-pick per preset policy
    batch_ref = Column(String, index=True, default="")
    status = Column(String, default="pending", index=True)  # pending|running|done|failed
    attempts = Column(Integer, default=0)
    last_error = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow, index=True)
    processed_at = Column(DateTime, nullable=True)


class Issue(Base):
    """TikTok-side problems found by the issue scan (rebuilt each scan):
    account status/payment trouble, rejected campaigns/ads, spark failures."""
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True)
    category = Column(String, index=True, default="")   # account | payment | campaign | ad | spark | bc
    level = Column(String, default="err")               # warn | err
    advertiser_id = Column(String, index=True, default="")
    advertiser_name = Column(String, default="")
    ref = Column(String, default="")                    # campaign/ad/spark id concerned
    message = Column(Text, default="")                  # plain-English problem
    detail = Column(Text, default="")                   # raw TikTok status/reason (copyable)
    detected_at = Column(DateTime, default=utcnow)


class PixelRecord(Base):
    """The pixel inventory — synced from accounts via /pixel/list/, plus the
    ones this tool created/transferred. owner_bc_id set = BC-owned (shareable)."""
    __tablename__ = "pixel_records"

    id = Column(Integer, primary_key=True)
    pixel_id = Column(String, unique=True, index=True, nullable=False)
    pixel_name = Column(String, default="")
    pixel_code = Column(String, default="")
    owner_advertiser_id = Column(String, default="")   # account it was found/created on
    owner_bc_id = Column(String, default="")           # set once moved into a BC
    synced_at = Column(DateTime, default=utcnow)


class SharedPixel(Base):
    """A pixel that was transferred to BC ownership for sharing."""
    __tablename__ = "shared_pixels"

    id = Column(Integer, primary_key=True)
    bc_id = Column(String, index=True, nullable=False)
    pixel_id = Column(String, nullable=False)
    pixel_name = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Small settings/log tables
# ---------------------------------------------------------------------------

class Setting(Base):
    """Generic key/value store (cookie health verdicts, sync stamps, UI prefs)."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class AppLog(Base):
    __tablename__ = "app_logs"

    id = Column(Integer, primary_key=True)
    level = Column(String, default="info")
    source = Column(String, default="")
    message = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow, index=True)


class EscapeTest(Base):
    """One row per phone that opened the in-app escape test page (/t/escape).
    Records what was tried and — via the landed page — where the visitor
    actually ended up (a real browser, or still inside TikTok's WebView)."""
    __tablename__ = "escape_tests"

    id = Column(Integer, primary_key=True)
    visit = Column(String, index=True, default="")      # random id minted by the test page
    platform = Column(String, default="")               # ios | android | other
    inapp = Column(Boolean, default=False)              # TikTok WebView detected on open
    app_version = Column(String, default="")            # TikTok app version from the UA, if present
    ua_open = Column(Text, default="")                  # user agent when the page opened
    method = Column(String, default="")                 # intent | x-safari | direct (no in-app browser) | none
    clicked = Column(Boolean, default=False)            # the visitor pressed the button
    landed_at = Column(DateTime, nullable=True)         # the landed page was reached
    ua_landed = Column(Text, default="")                # user agent where it landed — the proof
    outcome = Column(String, default="")                # escaped | stayed | no-click | lost
    created_at = Column(DateTime, default=utcnow, index=True)


class Appeal(Base):
    """One TikTok review rejection and what happened to its appeal
    (/adgroup/appeal/). TikTok allows ONE appeal per rejection and per ad group
    (Ads Manager hides the button once an ad group or any ad in it has been
    appealed), so a row is one AD GROUP + one rejection (audit_time), covering
    every rejected ad in it, and the engine never files twice for it.

    status: pending    — rejected, not appealed (auto-appeal off, daily cap hit)
            skipped    — rejected, not appealed: a skip keyword matched the reason
            appealing  — filed, waiting for TikTok
            successful — TikTok accepted the appeal (re-review follows)
            done       — re-reviewed after a successful appeal (see review_status)
            failed     — TikTok rejected the appeal
            error      — the appeal request itself failed (see error)
            cleared    — the ad is no longer rejected without an appeal (edited / approved / deleted)
            dismissed  — the operator chose not to appeal this rejection (sticks until a new review)"""
    __tablename__ = "appeals"

    id = Column(Integer, primary_key=True)
    advertiser_id = Column(String, index=True, default="")
    advertiser_name = Column(String, default="")
    campaign_id = Column(String, default="")
    campaign_name = Column(String, default="")
    adgroup_id = Column(String, index=True, default="")
    ad_id = Column(String, default="")                    # the ad the appeal targets; "" = the ad group itself
    ad_name = Column(String, default="")                  # rejected ad name(s), "; "-joined
    ads_n = Column(Integer, default=1)                    # how many rejected ads this row covers
    rejected_status = Column(String, default="")          # AD_STATUS_AUDIT_DENY / AD_STATUS_ADGROUP_AUDIT_DENY
    audit_time = Column(String, default="")               # TikTok's last_audit_time for THIS rejection (dedupe key)
    reasons = Column(Text, default="")                    # TikTok's rejection reasons ("; "-joined)
    suggestion = Column(Text, default="")                 # TikTok's review suggestion
    appeal_reason = Column(Text, default="")              # the text we sent
    filed_by = Column(String, default="")                 # auto | manual (Appeals page) | ads-manager (seen APPEALING, not ours)
    status = Column(String, index=True, default="pending")
    tiktok_status = Column(String, default="")            # raw appeal_status from /adgroup/review_info/
    review_status = Column(String, default="")            # ad group review_status after the appeal (ALL_AVAILABLE…)
    request_id = Column(String, default="")
    error = Column(Text, default="")
    attempts = Column(Integer, default=0)
    submitted_at = Column(DateTime, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)        # last scan that still reported the rejection
    gone = Column(Boolean, default=False)                 # a later scan no longer saw this rejection (a new one = new row)
    created_at = Column(DateTime, default=utcnow, index=True)


class PartnerSetup(Base):
    """One 'new partner' run: add a partner BC to the main BC (sharing chosen ad
    accounts), invite an email into a BC, then — once that member has joined —
    assign a TikTok account to them. The pixel / TikTok-account share with the
    PARTNER is website-only (no API), so it is a checklist the operator ticks.

    Step statuses: pending | done | error | skipped | waiting (member hasn't
    accepted the invite yet — re-checked every slow sweep)."""
    __tablename__ = "partner_setups"

    id = Column(Integer, primary_key=True)
    bc_id = Column(String, index=True, default="")          # main BC (owner of pixel + profile)
    bc_name = Column(String, default="")
    partner_id = Column(String, index=True, default="")     # the new partner BC
    partner_name = Column(String, default="")
    share_advertiser_ids = Column(Text, default="")         # comma list of main-BC ad accounts shared with the partner
    share_advertiser_role = Column(String, default="OPERATOR")
    partner_status = Column(String, default="pending")
    partner_error = Column(Text, default="")

    invite_bc_id = Column(String, default="")               # BC the email is invited into
    invite_bc_name = Column(String, default="")
    invite_email = Column(String, default="")
    invite_role = Column(String, default="STANDARD")        # ADMIN | STANDARD
    invite_advertiser_ids = Column(Text, default="")        # ad accounts of invite_bc pre-assigned to the member
    invite_advertiser_role = Column(String, default="OPERATOR")
    invite_status = Column(String, default="pending")
    invite_error = Column(Text, default="")

    tt_account_id = Column(String, default="")              # TikTok account (TT_ACCOUNT asset of invite_bc) to assign
    tt_account_name = Column(String, default="")
    tt_account_roles = Column(String, default="POST")       # comma list
    assign_status = Column(String, default="skipped")
    assign_error = Column(Text, default="")
    member_user_id = Column(String, default="")             # resolved from /bc/member/get/ once the invite is accepted
    member_status = Column(String, default="")              # relation_status as TikTok reports it

    pixel_shared = Column(Boolean, default=False)           # checklist: shared in Business Center by hand
    profile_shared = Column(Boolean, default=False)         # checklist: TikTok account shared by hand
    note = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow, index=True)
    updated_at = Column(DateTime, default=utcnow)


class MetricTick(Base):
    """One row per ACTIVE campaign per sync (~every minute on the fast sweep):
    the cumulative today-values TikTok reported at that moment. The Campaigns
    page turns consecutive ticks into pace — impressions / clicks / spend gained
    in the last 5, 15 and 60 minutes — so a bid change can be judged by whether
    delivery is actually moving. Pruned after PACE_KEEP_HOURS."""
    __tablename__ = "metric_ticks"

    id = Column(Integer, primary_key=True)
    advertiser_id = Column(String, default="")
    campaign_id = Column(String, index=True, nullable=False)
    at = Column(DateTime, default=utcnow, index=True)
    spend = Column(Float, default=0.0)
    impressions = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    conversions = Column(Integer, default=0)
