"""THE core: thin httpx wrapper over TikTok Marketing API v1.3.

Every call attaches the `Access-Token` header, passes `advertiser_id` where
required, and parses the `{code, message, data}` envelope. `code == 0` is
success; anything else raises TikTokError (see error_messages.py for the
plain-English mapping).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from . import config

BASE = config.TIKTOK_API_BASE
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class TikTokError(Exception):
    def __init__(self, code: Any, message: str, request_id: str = "", data: Any = None):
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data
        super().__init__(f"TikTok API error {code}: {message}")


def _parse(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except Exception:
        raise TikTokError("HTTP", f"Non-JSON response (HTTP {resp.status_code})", data=resp.text[:500])
    code = body.get("code")
    if code != 0:
        raise TikTokError(code, body.get("message", ""), body.get("request_id", ""), body.get("data"))
    return body.get("data", {})


def api_get(path: str, access_token: str, params: dict | None = None) -> Any:
    """GET — list params are JSON-encoded per TikTok convention."""
    q = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        q[k] = json.dumps(v) if isinstance(v, (list, dict)) else v
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(f"{BASE}{path}", params=q, headers={"Access-Token": access_token})
    return _parse(resp)


RETRYABLE_CODES = {"40100", "50000"}  # rate limit / TikTok internal error


def api_get_retry(path: str, access_token: str, params: dict | None = None,
                  attempts: int = 3, backoff: float = 0.6) -> Any:
    """api_get with retry+backoff on transient codes — used by the sync paths,
    where a rate limit must not silently drop a whole BC's account list."""
    import time as _time
    last: TikTokError | None = None
    for i in range(attempts):
        try:
            return api_get(path, access_token, params)
        except TikTokError as e:
            if str(e.code) not in RETRYABLE_CODES or i == attempts - 1:
                raise
            last = e
            _time.sleep(backoff * (i + 1))
    raise last  # pragma: no cover


def api_post(path: str, access_token: str, payload: dict) -> Any:
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BASE}{path}", json=payload,
            headers={"Access-Token": access_token, "Content-Type": "application/json"},
        )
    return _parse(resp)


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------

def exchange_auth_code(auth_code: str) -> dict:
    """POST /oauth2/access_token/ — exchange the callback auth_code for tokens."""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(f"{BASE}/oauth2/access_token/", json={
            "app_id": config.TIKTOK_APP_ID,
            "secret": config.TIKTOK_APP_SECRET,
            "auth_code": auth_code,
        })
    return _parse(resp)


def refresh_access_token(refresh_token: str) -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(f"{BASE}/oauth2/refresh_token/", json={
            "app_id": config.TIKTOK_APP_ID,
            "secret": config.TIKTOK_APP_SECRET,
            "refresh_token": refresh_token,
        })
    return _parse(resp)


def oauth_authorize_url(state: str) -> str:
    return (
        "https://business-api.tiktok.com/portal/auth"
        f"?app_id={config.TIKTOK_APP_ID}&state={state}"
        f"&redirect_uri={config.OAUTH_REDIRECT_URI}"
    )


# ---------------------------------------------------------------------------
# Business Center / accounts
# ---------------------------------------------------------------------------

def get_authorized_advertisers(access_token: str) -> list[dict]:
    """Advertisers the token can act on (works even without BC scope)."""
    data = api_get("/oauth2/advertiser/get/", access_token, {
        "app_id": config.TIKTOK_APP_ID, "secret": config.TIKTOK_APP_SECRET,
    })
    return data.get("list", [])


def list_business_centers(access_token: str) -> list[dict]:
    data = api_get_retry("/bc/get/", access_token, {"page": 1, "page_size": 50})
    return data.get("list", [])


def list_bc_advertisers(access_token: str, bc_id: str, page: int = 1, page_size: int = 100) -> dict:
    return api_get_retry("/bc/asset/get/", access_token, {
        "bc_id": bc_id, "asset_type": "ADVERTISER", "page": page, "page_size": page_size,
    })


def get_advertiser_info(access_token: str, advertiser_ids: list[str]) -> list[dict]:
    data = api_get_retry("/advertiser/info/", access_token, {"advertiser_ids": advertiser_ids})
    return data.get("list", [])


def get_bc_balance(access_token: str, bc_id: str) -> dict:
    """BC wallet balance. Returns the raw data dict; callers use
    parse_bc_balance() to get a float out of the varying shapes."""
    return api_get_retry("/bc/balance/get/", access_token, {"bc_id": bc_id})


def parse_bc_balance(data: dict) -> tuple[float, str]:
    """Defensive parse of /bc/balance/get/ payload -> (balance, currency)."""
    if not isinstance(data, dict):
        return 0.0, ""
    node = data
    for key in ("bc_balance", "balance_info", "data"):
        if isinstance(node.get(key), dict):
            node = node[key]
            break
    bal = node.get("balance", node.get("cash_balance", data.get("balance", 0)))
    cur = node.get("currency", data.get("currency", ""))
    try:
        return float(bal or 0), str(cur or "")
    except (TypeError, ValueError):
        return 0.0, str(cur or "")


def get_advertiser_balances(access_token: str, bc_id: str, page: int = 1,
                            page_size: int = 100) -> dict:
    """Ad account balances under a BC (`/advertiser/balance/get/`).
    Returns the raw page; the list items carry advertiser_id + balance."""
    return api_get_retry("/advertiser/balance/get/", access_token, {
        "bc_id": bc_id, "page": page, "page_size": page_size,
    })


def bc_transfer(access_token: str, bc_id: str, advertiser_id: str, amount: float,
                transfer_type: str = "RECHARGE") -> dict:
    """Move money between the BC wallet and one of its ad accounts.
    transfer_type: RECHARGE (BC -> account) | DEDUCT (account -> BC)."""
    return api_post("/bc/transfer/", access_token, {
        "bc_id": bc_id,
        "advertiser_id": advertiser_id,
        "transfer_type": transfer_type,
        "cash_amount": round(float(amount), 2),
    })


# ---------------------------------------------------------------------------
# BC pixel sharing (verified endpoints: bc/pixel/transfer, bc/pixel/link/*)
# ---------------------------------------------------------------------------

def bc_pixel_transfer(access_token: str, bc_id: str, advertiser_id: str,
                      pixel_id: str) -> dict:
    """Transfer a pixel from an ad account into Business Center ownership —
    prerequisite for linking it to other accounts."""
    return api_post("/bc/pixel/transfer/", access_token, {
        "bc_id": bc_id, "advertiser_id": advertiser_id, "pixel_id": pixel_id,
    })


def bc_pixel_link_update(access_token: str, bc_id: str, pixel_id: str,
                         advertiser_ids: list[str], operation: str = "LINK") -> dict:
    """Link (or UNLINK) a BC-owned pixel to ad accounts under the BC."""
    return api_post("/bc/pixel/link/update/", access_token, {
        "bc_id": bc_id, "pixel_id": pixel_id,
        "advertiser_ids": advertiser_ids, "operation": operation,
    })


def bc_pixel_link_get(access_token: str, bc_id: str, pixel_id: str,
                      page: int = 1, page_size: int = 100) -> dict:
    """Which ad accounts a BC-owned pixel is linked to."""
    return api_get("/bc/pixel/link/get/", access_token, {
        "bc_id": bc_id, "pixel_id": pixel_id, "page": page, "page_size": page_size,
    })


def list_regions(access_token: str, advertiser_id: str, placements: list[str] | None = None,
                 objective_type: str | None = None) -> list[dict]:
    """`/tool/region/` — discover targetable geo codes; cache on AdAccount."""
    params: dict = {"advertiser_id": advertiser_id,
                    "placements": placements or ["PLACEMENT_TIKTOK"]}
    if objective_type:
        params["objective_type"] = objective_type
    data = api_get("/tool/region/", access_token, params)
    return data.get("region_info", data.get("list", []))


# ---------------------------------------------------------------------------
# Campaign / ad group / ad
# ---------------------------------------------------------------------------

def create_campaign(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post("/campaign/create/", access_token, {"advertiser_id": advertiser_id, **payload})


def create_adgroup(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post("/adgroup/create/", access_token, {"advertiser_id": advertiser_id, **payload})


def create_ad(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post("/ad/create/", access_token, {"advertiser_id": advertiser_id, **payload})


def delete_campaigns(access_token: str, advertiser_id: str, campaign_ids: list[str]) -> dict:
    return api_post("/campaign/status/update/", access_token, {
        "advertiser_id": advertiser_id, "campaign_ids": campaign_ids, "operation_status": "DELETE",
    })


def update_campaign_status(access_token: str, advertiser_id: str, campaign_ids: list[str],
                           operation_status: str) -> dict:
    """operation_status: ENABLE | DISABLE | DELETE."""
    return api_post("/campaign/status/update/", access_token, {
        "advertiser_id": advertiser_id, "campaign_ids": campaign_ids,
        "operation_status": operation_status,
    })


def update_campaign_budget(access_token: str, advertiser_id: str, campaign_id: str,
                           budget: float) -> dict:
    """Change a CBO campaign's budget (/campaign/update/)."""
    return api_post("/campaign/update/", access_token, {
        "advertiser_id": advertiser_id, "campaign_id": campaign_id,
        "budget": round(float(budget), 2),
    })


def update_adgroup(access_token: str, advertiser_id: str, adgroup_id: str,
                   budget: float | None = None,
                   conversion_bid_price: float | None = None) -> dict:
    """Change an ad group's budget and/or cost cap (/adgroup/update/)."""
    payload: dict = {"advertiser_id": advertiser_id, "adgroup_id": adgroup_id}
    if budget is not None:
        payload["budget"] = round(float(budget), 2)
    if conversion_bid_price is not None:
        payload["conversion_bid_price"] = round(float(conversion_bid_price), 2)
        payload["bid_type"] = "BID_TYPE_CUSTOM"
    return api_post("/adgroup/update/", access_token, payload)


def list_campaigns(access_token: str, advertiser_id: str, page: int = 1, page_size: int = 100,
                   filtering: dict | None = None) -> dict:
    params: dict = {"advertiser_id": advertiser_id, "page": page, "page_size": page_size}
    if filtering:
        params["filtering"] = filtering
    return api_get("/campaign/get/", access_token, params)


def list_ads(access_token: str, advertiser_id: str, page: int = 1,
             page_size: int = 100, filtering: dict | None = None) -> dict:
    """Ads under an advertiser (/ad/get/) — carries secondary_status and,
    for rejected ads, the review reject reason fields."""
    params: dict = {"advertiser_id": advertiser_id, "page": page, "page_size": page_size}
    if filtering:
        params["filtering"] = filtering
    return api_get("/ad/get/", access_token, params)


def list_adgroups(access_token: str, advertiser_id: str, campaign_ids: list[str] | None = None,
                  page: int = 1, page_size: int = 100) -> dict:
    params: dict = {"advertiser_id": advertiser_id, "page": page, "page_size": page_size}
    if campaign_ids:
        params["filtering"] = {"campaign_ids": campaign_ids}
    return api_get("/adgroup/get/", access_token, params)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def get_report(access_token: str, advertiser_id: str, *, dimensions: list[str],
               metrics: list[str], start_date: str, end_date: str,
               data_level: str = "AUCTION_CAMPAIGN", page_size: int = 200) -> list[dict]:
    data = api_get("/report/integrated/get/", access_token, {
        "advertiser_id": advertiser_id,
        "report_type": "BASIC",
        "data_level": data_level,
        "dimensions": dimensions,
        "metrics": metrics,
        "start_date": start_date,
        "end_date": end_date,
        "page": 1,
        "page_size": page_size,
    })
    return data.get("list", [])


# ---------------------------------------------------------------------------
# Identities & Spark
# ---------------------------------------------------------------------------

def list_identities(access_token: str, advertiser_id: str, identity_type: str | None = None) -> list[dict]:
    """Identities connected to an account: TT_USER / BC_AUTH_TT / CUSTOMIZED_USER / AUTH_CODE.

    Crucial for spark launches (§9.2–9.3): the identity used must actually OWN
    the post being promoted.
    """
    params: dict = {"advertiser_id": advertiser_id, "page": 1, "page_size": 100}
    if identity_type:
        params["identity_type"] = identity_type
    data = api_get("/identity/get/", access_token, params)
    return data.get("identity_list", data.get("list", []))


def list_tt_videos(access_token: str, advertiser_id: str, identity_id: str,
                   identity_type: str, page: int = 1, page_size: int = 50) -> dict:
    """Lists a creator identity's AD-AUTHORIZED posts (⚠ §9.4 — nothing else).

    Each item carries item_info: auth_code, item_id, item_type (VIDEO/CAROUSEL)
    and cover image URLs. This powers Spark auto-grab.
    """
    return api_get("/identity/video/get/", access_token, {
        "advertiser_id": advertiser_id,
        "identity_id": identity_id,
        "identity_type": identity_type,
        "page": page,
        "page_size": page_size,
    })


def authorize_tt_video(access_token: str, advertiser_id: str, auth_code: str) -> dict:
    """Bind a spark auth code to an advertiser — returns identity_id + item_id.

    ⚠ §9.3: for BC-connected creators this may return a separate AUTH_CODE
    identity that does NOT own the post; verify ownership before using it.
    """
    return api_post("/tt_video/authorize/", access_token, {
        "advertiser_id": advertiser_id, "auth_code": auth_code,
    })


def get_tt_video_info(access_token: str, advertiser_id: str, item_ids: list[str],
                      identity_id: str | None = None, identity_type: str | None = None) -> list[dict]:
    params: dict = {"advertiser_id": advertiser_id, "item_ids": item_ids}
    if identity_id:
        params["identity_id"] = identity_id
        params["identity_type"] = identity_type or "AUTH_CODE"
    data = api_get("/tt_video/info/", access_token, params)
    return data.get("list", [])


def create_spark_identity(access_token: str, advertiser_id: str, display_name: str,
                          image_uri: str = "") -> dict:
    payload: dict = {"advertiser_id": advertiser_id, "display_name": display_name}
    if image_uri:
        payload["image_uri"] = image_uri
    return api_post("/identity/create/", access_token, payload)


def create_spark_ad(access_token: str, advertiser_id: str, payload: dict) -> dict:
    """Dedicated Spark Ad creation path.

    The generic /ad/create/ endpoint mishandles spark photo CAROUSELS — route
    spark creatives through here with identity + tiktok_item_id creatives.
    """
    return api_post("/ad/create/", access_token, {"advertiser_id": advertiser_id, **payload})


# ---------------------------------------------------------------------------
# Pixels, instant pages, lead forms
# ---------------------------------------------------------------------------

def list_pixels(access_token: str, advertiser_id: str, code: str | None = None) -> list[dict]:
    """Resolve a pixel CODE to its numeric pixel_id (§9.7). Cache results."""
    params: dict = {"advertiser_id": advertiser_id, "page": 1, "page_size": 100}
    if code:
        params["code"] = code
    data = api_get("/pixel/list/", access_token, params)
    return data.get("pixels", data.get("list", []))


def list_instant_pages(access_token: str, advertiser_id: str, page: int = 1,
                       page_size: int = 100, business_type: str = "LANDING_PAGE") -> dict:
    return api_get("/page/get/", access_token, {
        "advertiser_id": advertiser_id, "page": page, "page_size": page_size,
        "business_type": business_type,
    })


def list_lead_forms(access_token: str, advertiser_id: str, page: int = 1, page_size: int = 100) -> dict:
    return api_get("/page/get/", access_token, {
        "advertiser_id": advertiser_id, "page": page, "page_size": page_size,
        "business_type": "LEAD_GEN",
    })
