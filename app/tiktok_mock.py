"""TikTok TEST MODE — a simulated TikTok Marketing API for local development (v153).

Switched on by ADOPS_MOCK_TIKTOK=1, and REFUSED on a deployed server (config.ON_SERVER): there
it is ignored and logged, so a stray variable can never make a real dashboard pretend. When on,
every Marketing-API call (tiktok_api's one shared HTTP client) is answered here, in memory, with
deterministic data — nothing leaves the machine. Connect → sync → launch → review → numbers can
be exercised with no TikTok account at all. The page-editor web API (ads.tiktok.com cookies) is
NOT simulated.

Magic triggers (to rehearse the failure paths):
  failxN   in a campaign name  → its first N create attempts fail (40002)
  REJECT   in a campaign name  → its ads come back disapproved
  bad      in a spark code     → the code is refused ("auth code is invalid")
  outage   Diagnostics › Test mode, or set_outage(True) → every call fails (50000)
Ads sit in review for MOCK_REVIEW_S seconds (default 20), then deliver.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from urllib.parse import parse_qsl

BC_ID = "7000000000000000001"
ACCOUNTS = [
    ("7100000000000000001", "Mock · US account", "Etc/GMT+5", "US"),
    ("7100000000000000002", "Mock · UK account", "Europe/London", "GB"),
    ("7100000000000000003", "Mock · FR account", "Europe/Paris", "FR"),
]
IDENTITY = {"identity_id": "mock-bc-identity-1", "identity_type": "BC_AUTH_TT", "display_name": "mock.creator", "profile_image": ""}
PIXEL = {"pixel_id": "7300000000000000001", "pixel_code": "MOCKPIXEL01", "pixel_name": "Mock pixel", "events": []}
REGIONS = [("6252001", "United States", "US"), ("2635167", "United Kingdom", "GB"), ("3017382", "France", "FR"), ("2921044", "Germany", "DE")]
POSTS = [(str(7400000000000000001 + i), f"Mock post {i + 1}") for i in range(6)]
REVIEW_S = float(os.environ.get("MOCK_REVIEW_S", "20"))

_L = threading.Lock()
_S: dict = {}


def reset() -> None:
    with _L:
        _S.clear()
        _S.update({"n": 0, "outage": False, "failx": {}, "campaigns": {}, "adgroups": {}, "ads": {}, "calls": 0})


reset()


def set_outage(on: bool) -> bool:
    with _L:
        _S["outage"] = bool(on)
        return _S["outage"]


def status() -> dict:
    with _L:
        return {"outage": _S["outage"], "calls": _S["calls"], "campaigns": sum(len(v) for v in _S["campaigns"].values()),
                "ads": sum(len(v) for v in _S["ads"].values()), "review_s": REVIEW_S}


def _id(prefix: str) -> str:
    _S["n"] += 1
    return f"{prefix}{_S['n']:012d}"


def _h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def _ok(data) -> "tuple[int, dict]":
    return 200, {"code": 0, "message": "OK", "request_id": "mock-" + str(int(time.time() * 1000)), "data": data}


def _err(code: int, msg: str) -> "tuple[int, dict]":
    return 200, {"code": code, "message": msg, "request_id": "mock-err", "data": {}}


def _paged(items: list) -> dict:
    return {"list": items, "page_info": {"page": 1, "page_size": max(len(items), 1), "total_number": len(items), "total_page": 1}}


def _params(request) -> dict:
    out: dict = {}
    for k, v in parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes) else str(request.url.query)):
        try:
            out[k] = json.loads(v) if v[:1] in "[{" else v
        except ValueError:
            out[k] = v
    if request.method == "POST":
        ct = request.headers.get("content-type", "")
        if "json" in ct:
            try:
                out.update(json.loads(request.content or b"{}"))
            except ValueError:
                pass
    return out


def _metrics(cid: str) -> dict:
    """Deterministic numbers for a campaign (so a board looks alive and stays the same)."""
    h = _h(cid)
    impressions = 800 + h % 9000
    clicks = max(1, impressions // (40 + h % 60))
    spend = round(5 + (h % 4000) / 100, 2)
    conv = clicks // (8 + h % 12)
    return {"spend": str(spend), "impressions": str(impressions), "clicks": str(clicks), "conversion": str(conv),
            "ctr": str(round(clicks / impressions * 100, 2)), "cpc": str(round(spend / clicks, 2)),
            "cpm": str(round(spend / impressions * 1000, 2)), "cost_per_conversion": str(round(spend / conv, 2) if conv else 0)}


def _review_state(created: float, rejected: bool) -> str:
    if rejected:
        return "deny"
    return "audit" if time.time() - created < REVIEW_S else "ok"


def handle(request):
    """httpx.MockTransport handler."""
    import httpx
    status_code, body = route(request)
    return httpx.Response(status_code, json=body, request=request)


def route(request) -> "tuple[int, dict]":
    path = request.url.path
    path = path.split("/v1.3", 1)[1] if "/v1.3" in path else path
    with _L:
        _S["calls"] += 1
        if _S["outage"]:
            return _err(50000, "Mock outage: TikTok test mode is simulating an outage (Diagnostics › Test mode)")
        p = _params(request)
        adv = str(p.get("advertiser_id") or "")
        try:
            return _dispatch(path, p, adv)
        except Exception as e:      # noqa: BLE001 — a bug in the simulator reads as a TikTok error, never a crash
            return _err(40000, f"Mock simulator error on {path}: {type(e).__name__}: {e}")


def _dispatch(path: str, p: dict, adv: str) -> "tuple[int, dict]":
    # ---- auth / accounts ------------------------------------------------------------------
    if path in ("/oauth2/access_token/", "/oauth2/refresh_token/"):
        return _ok({"access_token": "mock-access-token", "refresh_token": "mock-refresh-token", "expires_in": 86400,
                    "refresh_token_expires_in": 31536000, "advertiser_ids": [a[0] for a in ACCOUNTS], "scope": []})
    if path == "/oauth2/advertiser/get/":
        return _ok({"list": [{"advertiser_id": a, "advertiser_name": n} for a, n, _, _ in ACCOUNTS]})
    if path == "/bc/get/":
        return _ok({"list": [{"bc_info": {"bc_id": BC_ID, "name": "Mock Business Center", "status": "ENABLE"}, "user_role": "ADMIN"}],
                    "page_info": {"page": 1, "total_page": 1}})
    if path == "/bc/asset/get/":
        return _ok(_paged([{"asset_id": a, "asset_name": n, "asset_type": "ADVERTISER", "advertiser_status": "STATUS_ENABLE",
                            "advertiser_role": "ADMIN"} for a, n, _, _ in ACCOUNTS]))
    if path == "/bc/balance/get/":
        return _ok({"balance": 5000.0, "currency": "USD", "cash_balance": 5000.0})
    if path == "/advertiser/info/":
        ids = p.get("advertiser_ids") or [a[0] for a in ACCOUNTS]
        by = {a[0]: a for a in ACCOUNTS}
        return _ok({"list": [{"advertiser_id": i, "name": by.get(i, (i, "Mock account"))[1], "status": "STATUS_ENABLE",
                              "currency": "USD", "timezone": by.get(i, (0, 0, "UTC"))[2], "country": by.get(i, (0, 0, 0, "US"))[3]}
                             for i in ids]})
    if path == "/advertiser/balance/get/":
        return _ok(_paged([{"advertiser_id": a, "advertiser_name": n, "balance": 250.0, "currency": "USD"} for a, n, _, _ in ACCOUNTS]))
    if path == "/tool/region/":
        return _ok({"region_info": [{"location_id": i, "name": n, "level": "COUNTRY", "region_code": c} for i, n, c in REGIONS]})
    # ---- identities / posts -----------------------------------------------------------------
    if path == "/identity/get/":
        t = p.get("identity_type")
        if t == "AUTH_CODE":
            return _ok({"identity_list": [], "page_info": {"page": 1, "total_page": 1}})
        return _ok({"identity_list": [IDENTITY], "page_info": {"page": 1, "total_page": 1}})
    if path == "/identity/video/get/":
        now = int(time.time())
        items = [{"item_info": {"item_id": iid, "text": txt, "item_type": "VIDEO", "auth_code": "",
                                "create_time": now - i * 86400 // 2, "video_info": {"duration": 12 + i}}}
                 for i, (iid, txt) in enumerate(POSTS)]
        return _ok({"list": items, "has_more": False, "cursor": None})
    if path in ("/tt_video/info/", "/tt_video/authorize/", "/identity/video/info/"):
        code = str(p.get("auth_code") or "")
        if "bad" in code.lower():
            return _err(40002, "The auth code is invalid (mock trigger: 'bad' in the code)")
        iid = POSTS[_h(code or "x") % len(POSTS)][0]
        return _ok({"list": [{"item_info": {"item_id": iid, "text": "Mock spark post", "item_type": "VIDEO", "auth_code": code},
                              "user_info": {"identity_id": "mock-code-identity", "identity_type": "AUTH_CODE", "display_name": "mock.creator"}}],
                    "identity_id": "mock-code-identity", "item_id": iid})
    if path == "/identity/create/":
        return _ok({"identity_id": _id("mock-identity-")})
    # ---- assets ------------------------------------------------------------------------------
    if path == "/pixel/list/":
        return _ok({"pixels": [PIXEL], "page_info": {"page": 1, "total_page": 1}})
    if path == "/page/get/":
        types = json.dumps(p.get("business_types") or p.get("business_type") or "")
        lead = "LEAD" in types
        return _ok(_paged([{"page_id": ("7600000000000000" if lead else "7500000000000000") + adv[-3:],
                            "title": "Mock Lead Form" if lead else "Mock Offer Page", "status": "PUBLISHED"}]))
    if path.startswith("/file/video/ad/upload"):
        return _ok([{"video_id": _id("v_mock_"), "video_cover_url": "", "poster_url": "", "width": 1080, "height": 1920}])
    if path.startswith("/file/image/ad/upload"):
        return _ok({"image_id": _id("ad-site-i18n-sg/mock"), "image_url": "", "width": 750, "height": 421})
    if path == "/file/video/suggestcover/":
        return _ok({"list": [{"id": _id("mock-cover-"), "url": ""}]})
    if path == "/creative/portfolio/create/":
        return _ok({"creative_portfolio_id": _id("7700")})
    # ---- campaigns ---------------------------------------------------------------------------
    if path in ("/campaign/create/", "/smart_plus/campaign/create/"):
        name = str(p.get("campaign_name") or "")
        m = re.search(r"failx(\d+)", name, re.I)
        if m:
            k = f"{adv}:{name}"
            _S["failx"][k] = _S["failx"].get(k, 0) + 1
            if _S["failx"][k] <= int(m.group(1)):
                return _err(40002, f"Mock failure {_S['failx'][k]} of {m.group(1)} (trigger 'failx{m.group(1)}' in the campaign name)")
        cid = _id("18")
        _S["campaigns"].setdefault(adv, []).append({
            "campaign_id": cid, "campaign_name": name, "advertiser_id": adv, "objective_type": p.get("objective_type", ""),
            "operation_status": "ENABLE", "secondary_status": "CAMPAIGN_STATUS_ENABLE", "budget": p.get("budget", 0),
            "budget_mode": p.get("budget_mode", "BUDGET_MODE_INFINITE"), "create_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "_t": time.time()})
        return _ok({"campaign_id": cid})
    if path in ("/adgroup/create/", "/smart_plus/adgroup/create/"):
        agid = _id("17")
        _S["adgroups"].setdefault(adv, []).append({"adgroup_id": agid, "campaign_id": str(p.get("campaign_id") or ""),
                                                  "adgroup_name": p.get("adgroup_name", ""), "operation_status": "ENABLE",
                                                  "budget": p.get("budget", 0), "_t": time.time()})
        return _ok({"adgroup_id": agid})
    if path in ("/ad/create/", "/smart_plus/ad/create/"):
        agid = str(p.get("adgroup_id") or "")
        ag = next((g for g in _S["adgroups"].get(adv, []) if g["adgroup_id"] == agid), None)
        cid = ag["campaign_id"] if ag else ""
        ids = []
        for c in (p.get("creatives") or [p]):
            ad_id = _id("16")
            ids.append(ad_id)
            _S["ads"].setdefault(adv, []).append({"ad_id": ad_id, "adgroup_id": agid, "campaign_id": cid,
                                                  "ad_name": (c or {}).get("ad_name", ""), "operation_status": "ENABLE", "_t": time.time()})
        return _ok({"ad_ids": ids, "ad_id": ids[0] if ids else ""})
    if path in ("/campaign/get/", "/smart_plus/campaign/get/"):
        out = []
        for c in _S["campaigns"].get(adv, []):
            rej = "reject" in c["campaign_name"].lower()
            st = _review_state(c["_t"], rej)
            sec = {"deny": "CAMPAIGN_STATUS_ENABLE", "audit": "CAMPAIGN_STATUS_ENABLE", "ok": "CAMPAIGN_STATUS_ENABLE"}[st]
            out.append({k: v for k, v in c.items() if not k.startswith("_")} | {"secondary_status": sec if c["operation_status"] == "ENABLE" else "CAMPAIGN_STATUS_DISABLE"})
        return _ok(_paged(out))
    if path == "/adgroup/get/":
        want = set(str(x) for x in ((p.get("filtering") or {}).get("campaign_ids") or []))
        camps = {c["campaign_id"]: c for c in _S["campaigns"].get(adv, [])}
        out = []
        for g in _S["adgroups"].get(adv, []):
            if want and g["campaign_id"] not in want:
                continue
            c = camps.get(g["campaign_id"], {})
            st = _review_state(g["_t"], "reject" in c.get("campaign_name", "").lower())
            out.append({k: v for k, v in g.items() if not k.startswith("_")} | {
                "secondary_status": {"deny": "ADGROUP_STATUS_AUDIT_DENY", "audit": "ADGROUP_STATUS_AUDIT", "ok": "ADGROUP_STATUS_DELIVERY_OK"}[st]})
        return _ok(_paged(out))
    if path == "/ad/get/":
        f = p.get("filtering") or {}
        want_ag = set(str(x) for x in f.get("adgroup_ids") or [])
        want_c = set(str(x) for x in f.get("campaign_ids") or [])
        camps = {c["campaign_id"]: c for c in _S["campaigns"].get(adv, [])}
        out = []
        for a in _S["ads"].get(adv, []):
            if (want_ag and a["adgroup_id"] not in want_ag) or (want_c and a["campaign_id"] not in want_c):
                continue
            st = _review_state(a["_t"], "reject" in camps.get(a["campaign_id"], {}).get("campaign_name", "").lower())
            out.append({k: v for k, v in a.items() if not k.startswith("_")} | {
                "secondary_status": {"deny": "AD_STATUS_AUDIT_DENY", "audit": "AD_STATUS_AUDIT", "ok": "AD_STATUS_DELIVERY_OK"}[st],
                **({"reject_reasons": ["Mock rejection (trigger 'REJECT' in the campaign name)"]} if st == "deny" else {})})
        return _ok(_paged(out))
    if path in ("/ad/review_info/", "/adgroup/review_info/"):
        return _ok({"list": []})
    if path.endswith("/status/update/"):
        status = str(p.get("operation_status") or p.get("opt_status") or "")
        ids = [str(x) for x in (p.get("campaign_ids") or p.get("adgroup_ids") or p.get("ad_ids") or [])]
        for bucket, key in (("campaigns", "campaign_id"), ("adgroups", "adgroup_id"), ("ads", "ad_id")):
            for row in _S[bucket].get(adv, []):
                if row[key] in ids and status in ("ENABLE", "DISABLE"):
                    row["operation_status"] = status
        return _ok({})
    if path == "/report/integrated/get/":
        rows = [{"dimensions": {"campaign_id": c["campaign_id"]}, "metrics": _metrics(c["campaign_id"])}
                for c in _S["campaigns"].get(adv, []) if _review_state(c["_t"], "reject" in c["campaign_name"].lower()) == "ok"]
        return _ok(_paged(rows))
    # everything else: an empty, successful answer (never a crash)
    return _ok(_paged([]))


def transport():
    import httpx
    return httpx.MockTransport(handle)
