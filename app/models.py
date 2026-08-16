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
    network = Column(String, nullable=False)               # everflow | cake | taprain
    sub_id = Column(String, default="")                    # maps back to campaign/account
    conversions = Column(Integer, default=0)
    revenue = Column(Float, default=0.0)
    sampled_at = Column(DateTime, default=utcnow, index=True)
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# VA time tracker
# ---------------------------------------------------------------------------

class TimeSession(Base):
    __tablename__ = "time_sessions"

    id = Column(Integer, primary_key=True)
    clock_in = Column(DateTime, nullable=False, default=utcnow)
    clock_out = Column(DateTime, nullable=True)
    hourly_rate = Column(Float, nullable=False, default=0.0)   # FROZEN at clock-in
    note = Column(String, default="")
    paid = Column(Boolean, default=False)

    breaks = relationship("TimeBreak", back_populates="session")


class TimeBreak(Base):
    __tablename__ = "time_breaks"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("time_sessions.id"), nullable=False)
    break_start = Column(DateTime, nullable=False, default=utcnow)
    break_end = Column(DateTime, nullable=True)

    session = relationship("TimeSession", back_populates="breaks")


class TimeSetting(Base):
    __tablename__ = "time_settings"

    id = Column(Integer, primary_key=True)
    hourly_rate = Column(Float, default=0.0)               # current rate; sessions stamp their own copy
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


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
    synced_at = Column(DateTime, default=utcnow)


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
