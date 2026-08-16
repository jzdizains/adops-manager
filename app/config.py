"""App configuration — everything secret comes from environment variables.

The new owner supplies (see README):
  TIKTOK_APP_ID / TIKTOK_APP_SECRET  — their TikTok Marketing API app
  OAUTH_REDIRECT_URI                 — must match the app registration EXACTLY
  APP_PASSWORD                       — the single operator login password
  SESSION_SECRET                     — random string signing the session cookie
  SECURITY_PIN                       — optional extra PIN gate ("" disables it)
  DATA_DIR                           — persistent disk path (Render: /data)
  BUSINESS_TZ                        — business timezone, default America/New_York
  EVERFLOW_API_KEY / CAKE_API_KEY / CAKE_API_URL / TAPRAIN_API_KEY — optional
"""
import os
from pathlib import Path

# --- TikTok Marketing API ---------------------------------------------------
TIKTOK_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
TIKTOK_APP_ID = os.environ.get("TIKTOK_APP_ID", "")
TIKTOK_APP_SECRET = os.environ.get("TIKTOK_APP_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback")

# --- App auth ---------------------------------------------------------------
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
SECURITY_PIN = os.environ.get("SECURITY_PIN", "")  # empty = PIN gate disabled

# --- Storage (MOUNT A DISK IN PROD — §9.8) ----------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "adops.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
COOKIE_FILE = DATA_DIR / "tiktok_cookies.json"   # pasted web cookies persist here

# --- Business timezone ------------------------------------------------------
BUSINESS_TZ = os.environ.get("BUSINESS_TZ", "America/New_York")

# --- Affiliate networks (optional) -------------------------------------------
EVERFLOW_API_KEY = os.environ.get("EVERFLOW_API_KEY", "")
CAKE_API_URL = os.environ.get("CAKE_API_URL", "")
CAKE_API_KEY = os.environ.get("CAKE_API_KEY", "")
TAPRAIN_API_KEY = os.environ.get("TAPRAIN_API_KEY", "")

# --- Misc --------------------------------------------------------------------
APP_NAME = "AdOps Manager"
STATIC_VERSION = "1"  # bump to cache-bust CSS/JS (§9.9)
