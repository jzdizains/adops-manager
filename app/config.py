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
HIGGSFIELD_KEY_ID = os.environ.get("HIGGSFIELD_KEY_ID", "")      # Higgsfield image models (api.higgsfield.ai)
HIGGSFIELD_KEY_SECRET = os.environ.get("HIGGSFIELD_KEY_SECRET", "")
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
# TEST_MODE relaxes security for the local test harness (plain-http cookies, placeholder
# password, optional 2FA, fast hashing). It must NEVER follow from ADOPS_DISABLE_BG alone —
# an operator who sets that on the server to stop the sweeps during an incident would
# silently switch off 2FA. So: tests opt in (ADOPS_TEST=1 or the legacy ADOPS_DISABLE_BG=1)
# and it is refused outright on a deployed host (Render sets RENDER; ADOPS_DEPLOYED for others).
ON_SERVER = bool(os.environ.get("RENDER") or os.environ.get("ADOPS_DEPLOYED"))
TEST_MODE = (os.environ.get("ADOPS_TEST") == "1" or os.environ.get("ADOPS_DISABLE_BG") == "1") and not ON_SERVER
# Login is refused while APP_PASSWORD / SESSION_SECRET are still the placeholders
# (a deployed app with "changeme" is open to anyone). Tests run with TEST_MODE.
ALLOW_INSECURE_DEFAULTS = TEST_MODE or os.environ.get("ALLOW_INSECURE_DEFAULTS") == "1"
SESSION_MAX_AGE_S = int(os.environ.get("SESSION_HOURS", "24")) * 3600     # every session ends after 24 h — log in (with the code) again
# 2FA is mandatory for every account (setup forced right after the first password login, the code
# asked at every login). Only the local test harness may switch it off; production never can.
REQUIRE_2FA = (not TEST_MODE) or os.environ.get("ADOPS_REQUIRE_2FA") == "1"
# TikTok TEST MODE (v153, tiktok_mock.py): every Marketing-API call answered by a local simulator.
# Local development only — refused on a deployed host, whatever the variable says.
MOCK_TIKTOK_ASKED = os.environ.get("ADOPS_MOCK_TIKTOK") == "1"
MOCK_TIKTOK = MOCK_TIKTOK_ASKED and not ON_SERVER
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
# Host the cookie web calls go to. Default: the same global host the browser uses —
# TikTok's edge routes each request to the session's own data centre (tt-target-idc,
# e.g. eu-ttp2) from the cookies, which are passed through untouched. Set this only if
# TikTok ever serves your region from a different hostname.
TIKTOK_ADS_WEB_HOST = (os.environ.get("TIKTOK_ADS_WEB_HOST") or "ads.tiktok.com").strip().replace("https://", "").strip("/")

# --- Business timezone ------------------------------------------------------
BUSINESS_TZ = os.environ.get("BUSINESS_TZ", "America/New_York")

# --- Background sync ---------------------------------------------------------
SYNC_INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MIN", "15"))
BC_LOW_BALANCE_THRESHOLD = float(os.environ.get("BC_LOW_BALANCE_THRESHOLD", "50"))

# --- Misc --------------------------------------------------------------------
APP_NAME = "AdOps Manager"
# X-Forwarded-For: how many proxies in front of the app APPEND to it (Render's edge = 1; add one
# for Cloudflare in front of Render). The client IP is read that many entries from the right.
try:
    TRUSTED_PROXY_HOPS = max(int(os.environ.get("TRUSTED_PROXY_HOPS", "1" if ON_SERVER else "0")), 0)
except ValueError:
    TRUSTED_PROXY_HOPS = 1
# extra origins allowed to POST to the dashboard (comma-separated, e.g. a second domain)
ALLOWED_ORIGINS = [o.strip().rstrip("/").lower() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

STATIC_VERSION = "172"  # bump to cache-bust CSS/JS (§9.9)


CODE_SUFFIXES = (".py", ".html", ".js", ".css")
MANIFEST = "build_manifest.json"     # written into app/ at packaging time (tools/package.py)


def code_files(root: Path | None = None) -> list[Path]:
    """The files the fingerprint covers. With app/build_manifest.json present (every
    delivered zip has one) it is exactly the files that zip shipped — so a leftover file
    from an older version sitting in the deploy repo cannot change the value, and a file
    the zip replaced but the repo still has old shows up as a mismatch, which is the point.
    Without a manifest (a working copy, the tests): every code file under app/."""
    root = root or Path(__file__).resolve().parent
    mf = root / MANIFEST
    if mf.exists():
        try:
            import json
            names = json.loads(mf.read_text(encoding="utf-8")).get("files") or []
            return [root / n for n in names]
        except (OSError, ValueError):
            pass
    return sorted(x for x in root.rglob("*") if x.suffix in CODE_SUFFIXES and "__pycache__" not in x.parts)


def build_id() -> str:
    """A short fingerprint of the code that is actually running — the first 7 hex chars
    of a SHA-1 over the code files (see code_files). Shown on Settings › Server and in
    /diagnostics.json so "did the deploy finish?" is answered by comparing two 7-character
    strings instead of by guessing from behaviour. Line endings are normalised first, so a
    Windows checkout (CRLF) fingerprints the same as the zip. Computed once, at first use
    (a few hundred KB read, then cached); a stale STATIC_VERSION can't fake it."""
    global _BUILD_ID
    if _BUILD_ID:
        return _BUILD_ID
    import hashlib
    h = hashlib.sha1()
    root = Path(__file__).resolve().parent
    for p in code_files(root):
        h.update(str(p.relative_to(root)).replace("\\", "/").encode()); h.update(b"\0")
        try:
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    _BUILD_ID = h.hexdigest()[:7]
    return _BUILD_ID


_BUILD_ID = ""
