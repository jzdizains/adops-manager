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
    status = Column(String, default="available")           # available | used
    used_advertiser_id = Column(String, default="")
    used_campaign_id = Column(String, default="")
    used_at = Column(DateTime, nullable=True)
    uploaded_at = Column(DateTime, default=utcnow)


class CreativeUpload(Base):
    """Cache: which TikTok video_id/cover a creative got in a given ad account
    (so a retried launch never re-uploads the file)."""
    __tablename__ = "creative_uploads"

    id = Column(Integer, primary_key=True)
    creative_id = Column(Integer, ForeignKey("creatives.id"), index=True)
    advertiser_id = Column(String, index=True)
    video_id = Column(String, default="")
    cover_image_id = Column(String, default="")
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
