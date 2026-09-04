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
    def __init__(self, code: Any, message: str, request_id: str = "", data: Any = None,
                 path: str = ""):
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data
        self.path = path            # endpoint that answered, e.g. "/ad/create/" — which STEP failed
        super().__init__(f"TikTok API error {code}: {message}")


def _endpoint(resp: httpx.Response) -> str:
    """'/ad/create/' from the response URL (…/open_api/v1.3/ad/create/)."""
    try:
        p = resp.request.url.path
    except Exception:
        return ""
    return p.split("/v1.3", 1)[1] if "/v1.3" in p else p


def _parse(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except Exception:
        raise TikTokError("HTTP", f"Non-JSON response (HTTP {resp.status_code})",
                          data=resp.text[:500], path=_endpoint(resp))
    code = body.get("code")
    if code != 0:
        raise TikTokError(code, body.get("message", ""), body.get("request_id", ""), body.get("data"),
                          path=_endpoint(resp))
    return body.get("data", {})


_client_lock = __import__("threading").Lock()
_shared_client: httpx.Client | None = None


def _client() -> httpx.Client:
    """ONE shared, pooled, thread-safe HTTP client for every TikTok call.

    We used to build (and tear down) a fresh Client + SSL context per call —
    thousands of times an hour with the background sync — which churned memory
    and helped ratchet RSS up to the host's limit. Keep-alive also makes the
    sync loops noticeably faster."""
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = httpx.Client(
                    timeout=TIMEOUT,
                    limits=httpx.Limits(max_connections=10,
                                        max_keepalive_connections=5,
                                        keepalive_expiry=30.0))
    return _shared_client


def api_get(path: str, access_token: str, params: dict | None = None) -> Any:
    """GET — list params are JSON-encoded per TikTok convention."""
    q = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        q[k] = json.dumps(v) if isinstance(v, (list, dict)) else v
    try:
        resp = _client().get(f"{BASE}{path}", params=q, headers={"Access-Token": access_token})
    except httpx.HTTPError as e:
        # network/timeout/proxy problems become a retryable "HTTP" TikTokError
        # instead of crashing whole sync loops with a raw transport exception
        raise TikTokError("HTTP", f"Network error calling TikTok: {e!r}")
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
    try:
        resp = _client().post(
            f"{BASE}{path}", json=payload,
            headers={"Access-Token": access_token, "Content-Type": "application/json"},
        )
    except httpx.HTTPError as e:
        raise TikTokError("HTTP", f"Network error calling TikTok: {e!r}")
    return _parse(resp)


# TikTok returns 40002 for BOTH permanent field errors and some transient
# backend hiccups (e.g. "Internal error, couldn't copy video data. Try again."
# right after a video upload). These phrases mark the transient kind — safe to
# retry regardless of the code.
_TRANSIENT_MSGS = ("internal error", "try again", "try later", "please retry",
                   "couldn't copy", "could not copy", "system busy", "timeout",
                   "timed out", "please try", "again later")


def _is_transient(e: "TikTokError") -> bool:
    if str(e.code) in ("40100", "50000", "HTTP"):
        return True
    msg = (e.message or "").lower()
    return any(m in msg for m in _TRANSIENT_MSGS)


def api_post_retry(path: str, access_token: str, payload: dict,
                   attempts: int = 5, backoff: float = 1.5) -> Any:
    """api_post with exponential backoff on transient failures — rate limits
    (40100), internal errors (50000), network blips (HTTP), and 40002 responses
    whose MESSAGE says the failure is transient ('Internal error… Try again',
    which TikTok returns while a freshly-uploaded video is still replicating).
    Absorbs these so a launch POST doesn't fail the whole account over a blip."""
    import time as _time
    last: TikTokError | None = None
    for i in range(attempts):
        try:
            return api_post(path, access_token, payload)
        except TikTokError as e:
            if not _is_transient(e) or i == attempts - 1:
                raise
            last = e
            _time.sleep(backoff * (2 ** i))   # 1.5s, 3s, 6s, 12s …
    raise last  # pragma: no cover


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------

def exchange_auth_code(auth_code: str) -> dict:
    """POST /oauth2/access_token/ — exchange the callback auth_code for tokens."""
    resp = _client().post(f"{BASE}/oauth2/access_token/", json={
        "app_id": config.TIKTOK_APP_ID,
        "secret": config.TIKTOK_APP_SECRET,
        "auth_code": auth_code,
    })
    return _parse(resp)


def refresh_access_token(refresh_token: str) -> dict:
    resp = _client().post(f"{BASE}/oauth2/refresh_token/", json={
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


def list_bc_advertisers(access_token: str, bc_id: str, page: int = 1, page_size: int = 50) -> dict:
    # ⚠ BC endpoints cap page_size at 50 — 100 gets a 40002 "must be most 50"
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
                            page_size: int = 50) -> dict:
    """Ad account balances under a BC (`/advertiser/balance/get/`).
    Returns the raw page; the list items carry advertiser_id + balance.
    ⚠ BC endpoints cap page_size at 50 — 100 gets a 40002 "must be most 50"."""
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


def pixel_update(access_token: str, advertiser_id: str, pixel_id: str,
                 pixel_name: str) -> dict:
    """Rename a pixel (POST /pixel/update/ — verified in TikTok's official SDK)."""
    return api_post("/pixel/update/", access_token, {
        "advertiser_id": advertiser_id, "pixel_id": pixel_id,
        "pixel_name": pixel_name,
    })


def pixel_create(access_token: str, advertiser_id: str, pixel_name: str,
                 pixel_category: str | None = None) -> dict:
    """Create a pixel on an ad account (POST /pixel/create/ — verified in
    TikTok's official SDK). Returns the new pixel's data (pixel_id, code)."""
    payload: dict = {"advertiser_id": advertiser_id, "pixel_name": pixel_name}
    if pixel_category:
        payload["pixel_category"] = pixel_category
    return api_post("/pixel/create/", access_token, payload)


def pixel_event_create(access_token: str, advertiser_id: str, pixel_id: str,
                       events: list[dict]) -> dict:
    """Define events on a pixel (POST /pixel/event/create/). Each event dict
    carries event_type (+ optional event_name / currency / rules)."""
    return api_post("/pixel/event/create/", access_token, {
        "advertiser_id": advertiser_id, "pixel_id": pixel_id,
        "pixel_events": events,
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


def track_event(access_token: str, pixel_code: str, event: str,
                event_id: str = "", ttclid: str = "", value: float = 0.0,
                currency: str = "USD", event_time: int | None = None,
                test_event_code: str = "", ip: str = "", user_agent: str = "",
                page_url: str = "") -> dict:
    """Events API `/event/track/` — server-side web pixel event (S2S postback
    → pixel). Needs at least one user identifier (ttclid is ours) or TikTok
    can't attribute it. event_id dedupes against browser-pixel events."""
    import time as _time
    user: dict = {}
    if ttclid:
        user["ttclid"] = ttclid
    if ip:
        user["ip"] = ip
    if user_agent:
        user["user_agent"] = user_agent
    item: dict = {"event": event, "event_time": int(event_time or _time.time())}
    if event_id:
        item["event_id"] = str(event_id)[:256]
    if user:
        item["user"] = user
    if value:
        item["properties"] = {"value": float(value), "currency": currency or "USD"}
    if page_url:
        item["page"] = {"url": page_url}
    payload: dict = {"event_source": "web", "event_source_id": pixel_code,
                     "data": [item]}
    if test_event_code:
        payload["test_event_code"] = test_event_code
    return api_post("/event/track/", access_token, payload)


# ---------------------------------------------------------------------------
# Campaign / ad group / ad
# ---------------------------------------------------------------------------

# the create endpoints retry transient rate limits (40100) with backoff so a
# burst during a batch launch doesn't fail the account outright
def create_campaign(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post_retry("/campaign/create/", access_token, {"advertiser_id": advertiser_id, **payload})


def create_adgroup(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post_retry("/adgroup/create/", access_token, {"advertiser_id": advertiser_id, **payload})


def create_ad(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post_retry("/ad/create/", access_token, {"advertiser_id": advertiser_id, **payload})


# ---------------------------------------------------------------------------
# Creative library uploads (SDK-verified: /file/video/ad/upload/,
# /file/image/ad/upload/, /identity/create/)
# ---------------------------------------------------------------------------

def upload_video_file(access_token: str, advertiser_id: str, file_path: str,
                      file_name: str) -> dict:
    """Upload a local video file into an ad account's asset library.
    Streams from disk — the video is never held in memory whole.
    Returns the first entry: {video_id, video_cover_url/poster_url, ...}."""
    import hashlib
    hasher = hashlib.md5()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    signature = hasher.hexdigest()
    try:
        with open(file_path, "rb") as fh:
            resp = _client().post(
                f"{BASE}/file/video/ad/upload/",
                headers={"Access-Token": access_token},
                data={"advertiser_id": advertiser_id, "upload_type": "UPLOAD_BY_FILE",
                      "video_signature": signature, "file_name": file_name,
                      "flaw_detect": "true", "auto_fix_enabled": "true",
                      "auto_bind_enabled": "true"},
                files={"video_file": (file_name, fh, "video/mp4")},
                timeout=httpx.Timeout(180.0, connect=10.0),
            )
    except (httpx.HTTPError, OSError) as e:
        raise TikTokError("HTTP", f"Network error uploading video: {e!r}")
    parsed = _parse(resp)
    if isinstance(parsed, list):
        return parsed[0] if parsed else {}
    return parsed


def upload_image_file(access_token: str, advertiser_id: str, file_path: str,
                      file_name: str) -> dict:
    """Upload a local image into an ad account's asset library
    (POST /file/image/ad/upload/, upload_type=UPLOAD_BY_FILE, image_signature=md5).
    Returns {image_id, image_url, ...}."""
    import hashlib
    with open(file_path, "rb") as fh:
        data = fh.read()
    signature = hashlib.md5(data).hexdigest()
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "png"
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/png")
    try:
        resp = _client().post(
            f"{BASE}/file/image/ad/upload/",
            headers={"Access-Token": access_token},
            data={"advertiser_id": advertiser_id, "upload_type": "UPLOAD_BY_FILE",
                  "image_signature": signature, "file_name": file_name},
            files={"image_file": (file_name, data, mime)},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
    except (httpx.HTTPError, OSError) as e:
        raise TikTokError("HTTP", f"Network error uploading image: {e!r}")
    parsed = _parse(resp)
    if isinstance(parsed, list):
        return parsed[0] if parsed else {}
    return parsed


# ---------------------------------------------------------------------------
# Music (doc: "Get the music list" v1.3 — GET /file/music/get/).
# For Carousel Ads, music_scene=CAROUSEL_ADS and search_type is REQUIRED:
#   SEARCH_BY_KEYWORD   filtering.keyword          paged, page_size 1-200
#   SEARCH_BY_RECOMMEND filtering.image_urls[2-35] TikTok picks music for the slides (no paging)
#   SEARCH_BY_SOURCE    filtering.sources=["USER"] the account's uploaded music
#   SEARCH_BY_LIKED / SEARCH_BY_HISTORY / SEARCH_BY_MUSIC_ID (filtering.music_ids)
# Response: data.musics[] {music_id, name, author, duration, url (12h), cover_url,
#           style, signature, sources, copyright, liked}, data.page_info
# ---------------------------------------------------------------------------
MUSIC_SEARCH_TYPES = ("SEARCH_BY_KEYWORD", "SEARCH_BY_RECOMMEND", "SEARCH_BY_SOURCE",
                      "SEARCH_BY_LIKED", "SEARCH_BY_HISTORY", "SEARCH_BY_MUSIC_ID")


def get_music(access_token: str, advertiser_id: str, search_type: str,
              keyword: str = "", image_urls: list[str] | None = None,
              music_ids: list[str] | None = None, sources: list[str] | None = None,
              page: int = 1, page_size: int = 100,
              music_scene: str = "CAROUSEL_ADS") -> dict:
    if search_type not in MUSIC_SEARCH_TYPES:
        raise TikTokError("APP", f"unknown music search_type {search_type}")
    params: dict = {"advertiser_id": advertiser_id, "music_scene": music_scene,
                    "search_type": search_type}
    filtering: dict = {}
    if search_type == "SEARCH_BY_KEYWORD":
        filtering["keyword"] = keyword
        params["page"], params["page_size"] = page, max(1, min(page_size, 200))
    elif search_type == "SEARCH_BY_RECOMMEND":
        filtering["image_urls"] = list(image_urls or [])
    elif search_type == "SEARCH_BY_SOURCE":
        filtering["sources"] = list(sources or ["USER"])
        params["page"], params["page_size"] = page, max(1, min(page_size, 200))
    elif search_type == "SEARCH_BY_MUSIC_ID":
        filtering["music_ids"] = list(music_ids or [])
    elif search_type == "SEARCH_BY_HISTORY":
        params["page"], params["page_size"] = page, max(1, min(page_size, 200))
    if filtering:
        params["filtering"] = filtering
    data = api_get("/file/music/get/", access_token, params)
    return data if isinstance(data, dict) else {"musics": []}


def recommend_ctas(access_token: str, advertiser_id: str, objective_type: str,
                   promotion_type: str, landing_page_url: str = "", ad_texts: list[str] | None = None,
                   optimization_goal: str = "", region_codes: list[str] | None = None,
                   language: str = "en") -> list[dict]:
    """Dynamic CTA candidates (doc "Get recommended CTAs", v1.3):
    GET /creative/cta/recommend/?new_version=true&objective_type=…&promotion_type=…
    → data.recommend_assets[] of {asset_ids: [..], asset_content: "Learn more"}."""
    params: dict = {"advertiser_id": advertiser_id, "new_version": "true",
                    "objective_type": objective_type, "promotion_type": promotion_type,
                    "language": language, "placements": ["PLACEMENT_TIKTOK"]}
    if landing_page_url:
        params["landing_page_url"] = landing_page_url
    if ad_texts:
        params["ad_texts"] = [t[:100] for t in ad_texts if t.strip()][:1]
    if optimization_goal:
        params["optimization_goal"] = optimization_goal
    if region_codes:
        params["region_codes"] = list(region_codes)
    data = api_get("/creative/cta/recommend/", access_token, params)
    out = []
    for a in ((data or {}).get("recommend_assets") or []):
        ids = [str(x) for x in (a.get("asset_ids") or []) if x]
        if ids and a.get("asset_content"):
            out.append({"asset_ids": ids, "asset_content": str(a["asset_content"])})
    return out


def create_cta_portfolio(access_token: str, advertiser_id: str, assets: list[dict]) -> str:
    """Dynamic CTA portfolio (doc "CTA recommendations → Dynamic CTAs", step 2):
    POST /creative/portfolio/create/ with creative_portfolio_type=CTA and one
    portfolio_content entry per recommended asset ({asset_content, asset_ids}).
    Returns creative_portfolio_id; ads then set call_to_action_id to it."""
    if not assets:
        raise TikTokError("APP", "no recommended CTAs to build a portfolio from")
    data = api_post("/creative/portfolio/create/", access_token, {
        "advertiser_id": advertiser_id, "creative_portfolio_type": "CTA",
        "portfolio_content": [{"asset_content": a["asset_content"], "asset_ids": list(a["asset_ids"])}
                              for a in assets]})
    pid = str((data or {}).get("creative_portfolio_id") or "")
    if not pid:
        raise TikTokError("APP", "CTA portfolio create returned no creative_portfolio_id")
    return pid


def upload_image_by_url(access_token: str, advertiser_id: str, image_url: str,
                        file_name: str = "") -> dict:
    """Upload an image (e.g. a video's poster/cover) by URL. Returns {image_id,...}."""
    payload: dict = {"advertiser_id": advertiser_id, "upload_type": "UPLOAD_BY_URL",
                     "image_url": image_url}
    if file_name:
        payload["file_name"] = file_name
    return api_post("/file/image/ad/upload/", access_token, payload)


def suggest_video_cover(access_token: str, advertiser_id: str, video_id: str,
                        page_size: int = 10) -> list[dict]:
    """`/file/video/suggestcover/` — TikTok's auto-generated cover frames for a
    video. Each item carries an `id` usable as the ad's cover image_id. The video
    must have finished processing, so callers retry while it's still encoding."""
    data = api_get("/file/video/suggestcover/", access_token, {
        "advertiser_id": advertiser_id, "video_id": video_id, "page_size": page_size})
    if isinstance(data, dict):
        return data.get("list") or data.get("suggest_cover") or data.get("covers") or []
    return data or []


# ---------------------------------------------------------------------------
# Smart+ (SDK-verified endpoints — /smart_plus/*, each create needs a client
# request_id for idempotency)
# ---------------------------------------------------------------------------

def _request_id() -> str:
    import uuid
    return uuid.uuid4().hex


def smart_plus_campaign_create(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post("/smart_plus/campaign/create/", access_token, {
        "advertiser_id": advertiser_id, "request_id": _request_id(), **payload})


def smart_plus_adgroup_create(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post("/smart_plus/adgroup/create/", access_token, {
        "advertiser_id": advertiser_id, "request_id": _request_id(), **payload})


def smart_plus_ad_create(access_token: str, advertiser_id: str, payload: dict) -> dict:
    return api_post("/smart_plus/ad/create/", access_token, {
        "advertiser_id": advertiser_id, **payload})


def smart_plus_campaign_get(access_token: str, advertiser_id: str, page: int = 1,
                            page_size: int = 100) -> dict:
    return api_get("/smart_plus/campaign/get/", access_token, {
        "advertiser_id": advertiser_id, "page": page, "page_size": page_size})


def smart_plus_campaign_status_update(access_token: str, advertiser_id: str,
                                      campaign_ids: list[str], operation_status: str) -> dict:
    """operation_status: ENABLE | DISABLE | DELETE (Smart+ campaigns only)."""
    return api_post("/smart_plus/campaign/status/update/", access_token, {
        "advertiser_id": advertiser_id, "campaign_ids": campaign_ids,
        "operation_status": operation_status,
    })


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


def update_campaign_name(access_token: str, advertiser_id: str, campaign_id: str,
                         campaign_name: str, smart_plus: bool = False) -> dict:
    """Rename a campaign (/campaign/update/ — or the Smart+ variant)."""
    path = "/smart_plus/campaign/update/" if smart_plus else "/campaign/update/"
    return api_post(path, access_token, {
        "advertiser_id": advertiser_id, "campaign_id": campaign_id,
        "campaign_name": campaign_name[:512],
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
    """ALL campaigns on an account — paginates so >100-campaign accounts never
    lose their newest campaigns off the end of page 1."""
    out: list = []
    while True:
        params: dict = {"advertiser_id": advertiser_id, "page": page, "page_size": page_size}
        if filtering:
            params["filtering"] = filtering
        data = api_get("/campaign/get/", access_token, params)
        batch = data.get("list", [])
        out.extend(batch)
        if len(batch) < page_size or page >= 30:   # 30 pages = 3000 campaigns
            return {"list": out}
        page += 1


def list_ads(access_token: str, advertiser_id: str, page: int = 1,
             page_size: int = 100, filtering: dict | None = None) -> dict:
    """Ads under an advertiser (/ad/get/) — carries secondary_status and,
    for rejected ads, the review reject reason fields."""
    params: dict = {"advertiser_id": advertiser_id, "page": page, "page_size": page_size}
    if filtering:
        params["filtering"] = filtering
    return api_get("/ad/get/", access_token, params)


def update_ad_landing_url(access_token: str, advertiser_id: str, adgroup_id: str,
                          ad_id: str, landing_page_url: str) -> dict:
    """Change ONE ad's landing page URL in place (/ad/update/ with
    patch_update=true, so only the field sent is touched — per the official SDK's
    AdUpdateBody {advertiser_id, adgroup_id, creatives[{ad_id, landing_page_url}],
    patch_update}). TikTok re-reviews the ad after a URL change."""
    return api_post("/ad/update/", access_token, {
        "advertiser_id": advertiser_id, "adgroup_id": adgroup_id, "patch_update": True,
        "creatives": [{"ad_id": ad_id, "landing_page_url": landing_page_url}],
    })


# ---------------------------------------------------------------------------
# Ad review + appeals (API Reference → Ad Review)
# ---------------------------------------------------------------------------

# Ad secondary statuses that mean "TikTok's review rejected it" and that the
# ad-group appeal endpoint covers. Account-level denials (ADVERTISER_AUDIT_DENY)
# and industry-qualification denials are different processes — not appealable here.
AD_REJECTED_STATUSES = ("AD_STATUS_AUDIT_DENY",)            # the ad itself failed review → appeal with ad_id
ADGROUP_REJECTED_STATUSES = ("AD_STATUS_ADGROUP_AUDIT_DENY",)  # the ad group failed review → appeal the ad group


def get_ad_review_info(access_token: str, advertiser_id: str, ad_ids: list[str]) -> dict:
    """GET /ad/review_info/ — per-ad review verdict with reject_info
    [{reasons[], suggestion, content_info}] and review_status
    (ALL_AVAILABLE / PART_AVAILABLE / UNAVAILABLE). 1–100 ad ids per call.
    Returns data.ad_review_map: {ad_id: {...}}."""
    out: dict = {}
    for i in range(0, len(ad_ids), 100):
        data = api_get("/ad/review_info/", access_token,
                       {"advertiser_id": advertiser_id, "ad_ids": [str(a) for a in ad_ids[i:i + 100]]})
        out.update((data or {}).get("ad_review_map") or {})
    return out


def get_adgroup_review_info(access_token: str, advertiser_id: str, adgroup_ids: list[str]) -> dict:
    """GET /adgroup/review_info/ — ad-group review verdict incl. appeal_status
    (NOT_APPEALED / APPEALING / APPEAL_SUCCESSFUL / APPEAL_FAILED / APPEAL_DONE),
    last_audit_time (UTC 'YYYY-MM-DD HH:MM:SS', real only when rejected),
    contain_rejected_ads, reject_info[]. Max 20 ad group ids per call.
    Returns {"groups": {adgroup_id: {...}}, "ads": {adgroup_id: {ad_id: {...}}}}."""
    groups: dict = {}
    ads: dict = {}
    for i in range(0, len(adgroup_ids), 20):
        data = api_get("/adgroup/review_info/", access_token,
                       {"advertiser_id": advertiser_id,
                        "adgroup_ids": [str(a) for a in adgroup_ids[i:i + 20]]}) or {}
        groups.update(data.get("ad_group_review_map") or {})
        ads.update(data.get("ad_review_map") or {})
    return {"groups": groups, "ads": ads}


def appeal_adgroup(access_token: str, advertiser_id: str, adgroup_id: str,
                   ad_id: str | None = None, appeal_reason: str = "",
                   attachment_list: list[str] | None = None) -> dict:
    """POST /adgroup/appeal/ — ask TikTok to re-evaluate a rejection. ad_id is
    optional: with it the appeal targets that ad, without it the ad group.
    TikTok allows ONE appeal per rejection (Ads Manager hides the button once an
    ad group or any ad in it has been appealed) — the caller must dedupe.
    Afterwards /adgroup/review_info/ reports appeal_status."""
    payload: dict = {"advertiser_id": advertiser_id, "adgroup_id": str(adgroup_id)}
    if ad_id:
        payload["ad_id"] = str(ad_id)
    if appeal_reason:
        payload["appeal_reason"] = appeal_reason
    if attachment_list:
        payload["attachment_list"] = list(attachment_list)
    return api_post("/adgroup/appeal/", access_token, payload)


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

def list_identities(access_token: str, advertiser_id: str, identity_type: str | None = None,
                    identity_authorized_bc_id: str = "") -> list[dict]:
    """Identities connected to an account: TT_USER / BC_AUTH_TT / AUTH_CODE.

    Crucial for spark launches (§9.2–9.3): the identity used must actually OWN
    the post being promoted. ⚠ identity_authorized_bc_id is REQUIRED by TikTok
    whenever identity_type is BC_AUTH_TT.
    """
    params: dict = {"advertiser_id": advertiser_id, "page": 1, "page_size": 100}
    if identity_type:
        params["identity_type"] = identity_type
    if identity_authorized_bc_id:
        params["identity_authorized_bc_id"] = identity_authorized_bc_id
    data = api_get("/identity/get/", access_token, params)
    return data.get("identity_list", data.get("list", []))


def list_tt_videos(access_token: str, advertiser_id: str, identity_id: str,
                   identity_type: str, page: int = 1, page_size: int = 50,
                   identity_authorized_bc_id: str = "") -> dict:
    """Lists a creator identity's AD-AUTHORIZED posts (⚠ §9.4 — nothing else).

    Each item carries item_info: auth_code, item_id, item_type (VIDEO/CAROUSEL)
    and cover image URLs. This powers Spark auto-grab.
    ⚠ identity_authorized_bc_id is REQUIRED when identity_type is BC_AUTH_TT.
    """
    params: dict = {
        "advertiser_id": advertiser_id,
        "identity_id": identity_id,
        "identity_type": identity_type,
        "page": page,
        "page_size": page_size,
    }
    if identity_authorized_bc_id:
        params["identity_authorized_bc_id"] = identity_authorized_bc_id
    data = api_get("/identity/video/get/", access_token, params)
    if not isinstance(data, dict):
        return {"list": [], "_keys": []}
    # normalize: TikTok has shipped the items under different keys over time
    items: list = []
    for key in ("list", "video_list", "item_list", "videos", "identity_video_list",
                "tiktok_item_list", "items"):
        v = data.get(key)
        if isinstance(v, list) and v:
            items = v
            break
    raw_keys = [k for k in data.keys() if k != "list"]
    data["list"] = items
    data["_keys"] = raw_keys
    return data


def tt_video_info(access_token: str, advertiser_id: str, auth_code: str = "",
                  item_id: str = "") -> dict:
    """Post info behind a spark auth code (/tt_video/info/ — the endpoint TikTok's
    SDK names as the source of `tiktok_item_id` for code-authorized sparks).
    Accepts either the auth code or a known item id."""
    params: dict = {"advertiser_id": advertiser_id}
    if auth_code:
        params["auth_code"] = auth_code
    if item_id:
        params["item_id"] = item_id
    return api_get("/tt_video/info/", access_token, params)


def identity_video_info(access_token: str, advertiser_id: str, identity_id: str,
                        identity_type: str, item_id: str,
                        identity_authorized_bc_id: str = "") -> dict:
    """Direct ownership probe (/identity/video/info/ — SDK-verified): succeeds
    only when THIS identity can use THIS post. No listing involved."""
    params: dict = {
        "advertiser_id": advertiser_id, "identity_id": identity_id,
        "identity_type": identity_type, "item_id": item_id,
    }
    if identity_authorized_bc_id:
        params["identity_authorized_bc_id"] = identity_authorized_bc_id
    return api_get("/identity/video/info/", access_token, params)


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
    return api_post_retry("/ad/create/", access_token, {"advertiser_id": advertiser_id, **payload})


# ---------------------------------------------------------------------------
# Pixels, instant pages, lead forms
# ---------------------------------------------------------------------------

def list_pixels(access_token: str, advertiser_id: str, code: str | None = None) -> list[dict]:
    """All pixels on an account (§9.7). ⚠ /pixel/list/ caps page_size at 20 —
    paginate to collect everything."""
    out: list[dict] = []
    page = 1
    while True:
        params: dict = {"advertiser_id": advertiser_id, "page": page, "page_size": 20}
        if code:
            params["code"] = code
        data = api_get("/pixel/list/", access_token, params)
        batch = data.get("pixels", data.get("list", []))
        out.extend(batch)
        if len(batch) < 20 or page >= 25:   # 25 pages = 500 pixels, plenty
            return out
        page += 1


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
