"""App configuration — everything secret comes from environment variables.

The new owner supplies (see README):
  TIKTOK_APP_ID / TIKTOK_APP_SECRET  — their TikTok Marketing API app
  OAUTH_REDIRECT_URI                 — must match the app registration EXACTLY
  APP_PASSWORD                       — the single operator login password
  SESSION_SECRET                     — random string signing the session cookie
  SECURITY_PIN                       — optional extra PIN gate ("" disables it)
  DATA_DIR                           — persistent disk path (Render: /data)
  BUSINESS_TZ                        — business timezone, default America/New_York
"""
import os
from pathlib import Path

# --- TikTok Marketing API ---------------------------------------------------
TIKTOK_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
TIKTOK_APP_ID = os.environ.get("TIKTOK_APP_ID", "")
TIKTOK_APP_SECRET = os.environ.get("TIKTOK_APP_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback")

# --- TensorPix (creative enhancement / variation) --------------------------
TENSORPIX_API_KEY = os.environ.get("TENSORPIX_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")          # Nano Banana image editing
TENSORPIX_BASE = os.environ.get("TENSORPIX_BASE", "https://backend.tensorpix.ai")

# --- App auth ---------------------------------------------------------------
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
SECURITY_PIN = os.environ.get("SECURITY_PIN", "")  # empty = PIN gate disabled
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@adops.local")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")   # Assistant page — set in Render, never in code
RESET_OWNER_PASSWORD = os.environ.get("RESET_OWNER_PASSWORD", "") == "1"   # lockout escape hatch: owner's password := APP_PASSWORD at next start
IPINFO_TOKEN = os.environ.get("IPINFO_TOKEN", "")   # optional: ipinfo.io token for IP → location on the access log (works tokenless at a low daily limit)   # the first (admin) account, minted from APP_PASSWORD on first start
ALLOWED_IPS = os.environ.get("ALLOWED_IPS", "")    # optional: comma-separated IPs/CIDRs allowed to log in ("" = any)
TEST_MODE = os.environ.get("ADOPS_DISABLE_BG") == "1"   # local tests: plain-http cookies, default password tolerated
# Login is refused while APP_PASSWORD / SESSION_SECRET are still the placeholders
# (a deployed app with "changeme" is open to anyone). Tests run with TEST_MODE.
ALLOW_INSECURE_DEFAULTS = TEST_MODE or os.environ.get("ALLOW_INSECURE_DEFAULTS") == "1"
SESSION_MAX_AGE_S = int(os.environ.get("SESSION_HOURS", "24")) * 3600     # every session ends after 24 h — log in (with the code) again
# 2FA is mandatory for every account (setup forced right after the first password login, the code
# asked at every login). Only the local test harness may switch it off; production never can.
REQUIRE_2FA = (not TEST_MODE) or os.environ.get("ADOPS_REQUIRE_2FA") == "1"
# Hide the front door: with LOGIN_PATH set (e.g. "/door-7f3k2"), the login page lives ONLY there,
# /login and every other unauthenticated URL answer 404 — a visitor who only knows the domain
# (say, from a postback URL) sees nothing. POSTBACK_HOST: a second hostname pointed at this
# service that serves ONLY /postback (+ /health) — the dashboard is not reachable through it.
LOGIN_PATH = os.environ.get("LOGIN_PATH", "/login").strip() or "/login"
if not LOGIN_PATH.startswith("/"):
    LOGIN_PATH = "/" + LOGIN_PATH
POSTBACK_HOST = os.environ.get("POSTBACK_HOST", "").strip().lower().split(":")[0]

# --- Storage (MOUNT A DISK IN PROD — §9.8) ----------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "adops.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
COOKIE_FILE = DATA_DIR / "tiktok_cookies.json"   # pasted web cookies persist here

# --- Business timezone ------------------------------------------------------
BUSINESS_TZ = os.environ.get("BUSINESS_TZ", "America/New_York")

# --- Background sync ---------------------------------------------------------
SYNC_INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MIN", "15"))
BC_LOW_BALANCE_THRESHOLD = float(os.environ.get("BC_LOW_BALANCE_THRESHOLD", "50"))

# --- Misc --------------------------------------------------------------------
APP_NAME = "AdOps Manager"
STATIC_VERSION = "43"  # bump to cache-bust CSS/JS (§9.9)
