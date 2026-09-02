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
STATIC_VERSION = "17"  # bump to cache-bust CSS/JS (§9.9)
