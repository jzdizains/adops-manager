"""Cookie-authenticated ads.tiktok.com web calls — the SECOND auth path.

A few things have no Marketing-API endpoint (cloning instant pages, some
lead-form reads, carousel web-chain launches). For those we replay
authenticated calls to ads.tiktok.com using the operator's browser cookies +
csrftoken, pasted on the TikTok Cookies page and persisted to the data disk.

⚠ §9.5 — TikTok Ads SSO login sets the `_ads`-suffixed cookie family
(`sid_guard_ads`, `sid_ucp_sso_v1_ads`, `sso_uid_tt_ads`, `sso_user_ads`),
NOT a plain `sessionid`. Validation must accept EITHER family (and always
require `csrftoken`).

⚠ §9.6 — a permission error (40002/40102) on a probe means the session is
LIVE but lacks rights to that advertiser. Genuine expiry looks like a login
redirect, non-JSON HTML, or code 200000.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from . import config

ADS_BASE = "https://ads.tiktok.com"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Either of these cookie families marks a logged-in session (§9.5)
CLASSIC_SESSION_COOKIES = {"sessionid", "sessionid_ss"}
ADS_SSO_COOKIES = {"sid_guard_ads", "sid_ucp_sso_v1_ads", "sso_uid_tt_ads", "sso_user_ads"}


class WebAuthError(Exception):
    pass


# ---------------------------------------------------------------------------
# Cookie storage (persisted on the data disk — survives redeploys, §9.8)
# ---------------------------------------------------------------------------

def save_cookies(raw: str) -> dict:
    """Accepts a Cookie-Editor JSON export (list of {name, value, ...}) or a
    plain `k=v; k2=v2` header string. Normalizes to {name: value} and persists.
    Returns the validation verdict."""
    cookies = _parse_cookie_input(raw)
    verdict = validate_cookies(cookies)
    if not verdict["ok"]:
        raise WebAuthError(verdict["reason"])
    payload = {"cookies": cookies, "saved_at": datetime.now(timezone.utc).isoformat()}
    config.COOKIE_FILE.write_text(json.dumps(payload, indent=2))
    return verdict


def load_cookies() -> dict[str, str]:
    if not config.COOKIE_FILE.exists():
        return {}
    try:
        return json.loads(config.COOKIE_FILE.read_text()).get("cookies", {})
    except Exception:
        return {}


def cookies_saved_at() -> str:
    if not config.COOKIE_FILE.exists():
        return ""
    try:
        return json.loads(config.COOKIE_FILE.read_text()).get("saved_at", "")
    except Exception:
        return ""


def _parse_cookie_input(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        raise WebAuthError("No cookies pasted.")
    if raw.startswith("["):  # Cookie-Editor export
        try:
            items = json.loads(raw)
            return {c["name"]: c["value"] for c in items if c.get("name")}
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            raise WebAuthError(f"Could not parse the Cookie-Editor export: {e}")
    if raw.startswith("{"):  # already a {name: value} map
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except json.JSONDecodeError as e:
            raise WebAuthError(f"Could not parse the JSON: {e}")
    # header string fallback
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    if not out:
        raise WebAuthError("Unrecognized cookie format — paste a Cookie-Editor JSON export.")
    return out


def validate_cookies(cookies: dict[str, str]) -> dict:
    """Structural validation. Accept the classic OR the Ads-SSO family (§9.5);
    always require csrftoken."""
    names = set(cookies)
    has_classic = bool(names & CLASSIC_SESSION_COOKIES)
    has_sso = bool(names & ADS_SSO_COOKIES)
    if not (has_classic or has_sso):
        return {"ok": False, "family": None,
                "reason": "No TikTok session cookie found. Expected `sessionid` or the Ads-SSO "
                          "family (sid_guard_ads / sid_ucp_sso_v1_ads / sso_uid_tt_ads / sso_user_ads). "
                          "Export cookies from ads.tiktok.com while logged in."}
    if "csrftoken" not in names:
        return {"ok": False, "family": "sso" if has_sso else "classic",
                "reason": "Missing `csrftoken` cookie — export ALL cookies for ads.tiktok.com, not just the session."}
    return {"ok": True, "family": "sso" if has_sso else "classic", "reason": ""}


# ---------------------------------------------------------------------------
# Authenticated web calls
# ---------------------------------------------------------------------------

def _client(cookies: dict[str, str] | None = None) -> httpx.Client:
    cookies = cookies if cookies is not None else load_cookies()
    if not cookies:
        raise WebAuthError("No TikTok web cookies stored — paste them on the TikTok Cookies page.")
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Referer": f"{ADS_BASE}/",
        "X-CSRFToken": cookies.get("csrftoken", ""),
    }
    return httpx.Client(base_url=ADS_BASE, cookies=cookies, headers=headers,
                        timeout=TIMEOUT, follow_redirects=False)


def web_get(path: str, params: dict | None = None) -> dict:
    with _client() as c:
        resp = c.get(path, params=params or {})
    return _web_parse(resp)


def web_post(path: str, payload: dict) -> dict:
    with _client() as c:
        resp = c.post(path, json=payload)
    return _web_parse(resp)


def _web_parse(resp: httpx.Response) -> dict:
    # A login redirect = genuine expiry (§9.6)
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location", "")
        raise WebAuthError(f"Session expired — TikTok redirected to login ({loc[:120]}). Paste fresh cookies.")
    try:
        body = resp.json()
    except Exception:
        raise WebAuthError(f"Session expired — TikTok returned an HTML page (HTTP {resp.status_code}) "
                           "instead of JSON. Paste fresh cookies.")
    code = body.get("code", 0)
    if str(code) == "200000":
        raise WebAuthError("Session expired (TikTok code 200000: 'Please log into your user account'). "
                           "Paste fresh cookies.")
    return body


# ---------------------------------------------------------------------------
# Health probe (§9.6)
# ---------------------------------------------------------------------------

def probe_health(own_advertiser_id: str | None = None) -> dict:
    """Probe cookie health against one of the operator's OWN accounts.

    Verdicts: `live` (session works), `live_no_permission` (session works but
    that advertiser isn't covered — still LIVE), `expired`, `missing`.
    """
    cookies = load_cookies()
    if not cookies:
        return {"verdict": "missing", "detail": "No cookies stored yet."}
    structural = validate_cookies(cookies)
    if not structural["ok"]:
        return {"verdict": "missing", "detail": structural["reason"]}
    try:
        params = {"aadvid": own_advertiser_id} if own_advertiser_id else {}
        body = web_get("/api/v2/i18n/account/info/", params=params)
        code = str(body.get("code", "0"))
        if code in ("40002", "40102"):
            # permission ≠ expiry: the session is alive (§9.6)
            return {"verdict": "live_no_permission", "family": structural["family"],
                    "detail": "Session live, but no permission for the probed advertiser."}
        return {"verdict": "live", "family": structural["family"],
                "detail": f"Session live (code {code})."}
    except WebAuthError as e:
        return {"verdict": "expired", "family": structural["family"], "detail": str(e)}
    except httpx.HTTPError as e:
        return {"verdict": "unknown", "family": structural["family"],
                "detail": f"Network problem while probing: {e}"}


# ---------------------------------------------------------------------------
# Web-only operations
# ---------------------------------------------------------------------------

def clone_instant_page(page_id: str, from_advertiser_id: str, to_advertiser_id: str) -> dict:
    """Clone an instant page to another advertiser — no Marketing-API endpoint
    exists for this, so it rides the cookie web path."""
    return web_post("/api/v1/page/copy/", {
        "page_id": page_id,
        "aadvid": from_advertiser_id,
        "target_aadvid": to_advertiser_id,
    })


def web_list_lead_forms(advertiser_id: str) -> dict:
    """Some lead-form reads only exist on the web API."""
    return web_get("/api/v1/page/list/", {"aadvid": advertiser_id, "business_type": "LEAD_GEN"})
