"""TikTok's Lead Generation Terms, per ad account (v155.13).

What was found (24 Sep 2026, recorded in Ads Manager while an Instant Form lead campaign was built
by hand on blue bat_260706030021): after the campaign is created, Ads Manager silently POSTs

    /mi/api/v3/i18n/agreement/general_sign/?aadvid=<adv>&req_src=ad_creation
        {"setting_type": 16, "setting_dimension": 1}
        {"setting_type": 70, "setting_dimension": 2}

— no pop-up, no checkbox. Accounts where a lead ad was built by hand carry both (query:
/api/v3/i18n/agreement/general_query/?aadvid=&setting_type=&setting_dimension= → is_exist true);
the account where the API was refused with "Lead Generation agreement has not be signed yet" carries
neither. The official API has no way to sign it, so an Instant Form ad from the dashboard is refused
until it is.

This module reads that state and — only when the operator presses the button that says so, with the
terms linked — signs it the way Ads Manager does, through the logged-in TikTok web session (the same
cookies the form builder uses). Nothing here runs by itself.
"""
from __future__ import annotations

import threading
import time

from . import spark_web_api

QUERY = "/api/v3/i18n/agreement/general_query/"
SIGN = "/mi/api/v3/i18n/agreement/general_sign/"
AGREEMENTS = ((16, 1), (70, 2))          # both, as Ads Manager signs them at the first lead ad
TERMS_URL = "https://ads.tiktok.com/i18n/official/policy/lead-gen-terms"
OK_TTL = 24 * 3600                        # a signed agreement stays signed
NO_TTL = 300                              # re-check an unsigned / unknown account after 5 min
_MAX = 2000

_cache: dict = {}                         # advertiser_id → (expires, True | False)
_lock = threading.Lock()


def _referer(adv: str) -> dict:
    return {"Referer": f"{spark_web_api.ADS_BASE}/i18n/manage/campaign?aadvid={adv}"}


def _is_signed(adv: str, st: int, dim: int) -> bool:
    body = spark_web_api._web_parse(spark_web_api._send(
        "GET", QUERY, params={"aadvid": adv, "setting_type": st, "setting_dimension": dim}, headers=_referer(adv)), QUERY)
    if str(body.get("code", "")) != "0":
        raise ValueError(f"{body.get('code')} {body.get('msg') or ''}".strip())
    return bool((body.get("data") or {}).get("is_exist"))


def cached(adv: str):
    with _lock:
        hit = _cache.get(str(adv))
    return hit[1] if hit and hit[0] > time.time() else None


def _remember(adv: str, ok: bool) -> None:
    with _lock:
        if len(_cache) >= _MAX:
            _cache.clear()
        _cache[str(adv)] = (time.time() + (OK_TTL if ok else NO_TTL), ok)


def status(adv: str, fresh: bool = False):
    """True = accepted, False = not yet, None = couldn't tell (no cookies, expired, or TikTok
    answered something unexpected — the launch then simply tries). Raises nothing."""
    adv = str(adv)
    if not fresh:
        c = cached(adv)
        if c is not None:
            return c
    if not spark_web_api.load_cookies():
        return None
    try:
        ok = all(_is_signed(adv, st, dim) for st, dim in AGREEMENTS)
    except Exception:      # noqa: BLE001 — WebAuthError, network, unexpected answer
        return None
    _remember(adv, ok)
    return ok


def accept(adv: str) -> tuple[bool, str]:
    """Sign what is missing, exactly as Ads Manager does, then read it back. (ok, message)."""
    adv = str(adv)
    if not spark_web_api.load_cookies():
        return False, "paste the ads.tiktok.com cookies on the TikTok Cookies page first"
    try:
        for st, dim in AGREEMENTS:
            if _is_signed(adv, st, dim):
                continue
            body = spark_web_api._web_parse(spark_web_api._send(
                "POST", SIGN, params={"aadvid": adv, "req_src": "ad_creation"},
                payload={"setting_type": st, "setting_dimension": dim}, headers=_referer(adv)), SIGN)
            if str(body.get("code", "")) != "0":
                return False, f"TikTok refused ({body.get('code')} {body.get('msg') or ''})".strip()
        ok = all(_is_signed(adv, st, dim) for st, dim in AGREEMENTS)
    except spark_web_api.WebAuthError as e:
        return False, str(e)[:200]
    except Exception as e:  # noqa: BLE001
        return False, f"couldn't reach TikTok ({type(e).__name__})"
    _remember(adv, ok)
    return (True, "accepted") if ok else (False, "TikTok answered success but the account still reads as not accepted")
