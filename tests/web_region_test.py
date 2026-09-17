"""Cookie web path — region routing (v120).

The tool never chose a region: every stored cookie (tt-target-idc, tt-target-idc-sign,
store-idc, …) goes to the global host untouched, and TikTok's edge routes the request to
the session's own data centre. What WAS missing: any redirect was called "session
expired", nothing was logged, and a refusal was reported as success. Now, against a fake
TikTok (httpx MockTransport):
- the region cookies reach TikTok exactly as exported
- session_region() reads the IDC from the cookies; the host is configurable
- a same-site, non-login redirect is followed once with the same cookies (and logged)
- a login redirect is still an expiry
- a non-zero TikTok code is returned to the caller AND logged with host + idc
- the clone routes report TikTok's refusal instead of "accepted"
Runs with httpx installed."""
import json, os, sys, tempfile, types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m; return m

try:
    import httpx
except ImportError:
    print("SKIP: httpx not installed"); sys.exit(0)

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
cf = Path(tempfile.mkdtemp()) / "cookies.json"
_mod("app.config", COOKIE_FILE=cf, TIKTOK_ADS_WEB_HOST="ads.tiktok.com")
notes = []
_mod("app.diag", record=lambda *a, **k: notes.append(a))
import importlib
w = importlib.import_module("app.spark_web_api")

COOKIES = {"sessionid_ads": "s", "sid_guard_ads": "g", "csrftoken": "c", "tt-target-idc": "eu-ttp2",
           "tt-target-idc-sign": "SIGNED.abc", "store-idc": "eu-ttp2", "store-country-code": "lv"}
cf.write_text(json.dumps({"cookies": COOKIES, "saved_at": "now"}))

seen = []
def fake(handler):
    """Install a MockTransport-backed client so no real network is touched."""
    def _client(cookies=None):
        cookies = cookies if cookies is not None else w.load_cookies()
        headers = {"X-CSRFToken": cookies.get("csrftoken", ""), "Referer": w.ADS_BASE + "/"}
        return httpx.Client(base_url=w.ADS_BASE, cookies=cookies, headers=headers, transport=httpx.MockTransport(handler), follow_redirects=False)
    w._client = _client

print("\n-- cookies pass through untouched --")
def h_ok(req):
    seen.append(req)
    return httpx.Response(200, json={"code": 0, "msg": "OK", "data": {"x": 1}})
fake(h_ok); seen.clear(); notes.clear()
body = w.web_post("/api/v1/page/copy/", {"page_id": "p", "aadvid": "1", "target_aadvid": "2"})
ck = seen[0].headers.get("cookie", "")
check("request went to the global host with the region cookies exactly as stored", seen[0].url.host == "ads.tiktok.com" and "tt-target-idc=eu-ttp2" in ck and "tt-target-idc-sign=SIGNED.abc" in ck and "store-idc=eu-ttp2" in ck)
check("csrf header + JSON body", seen[0].headers.get("x-csrftoken") == "c" and json.loads(seen[0].content)["target_aadvid"] == "2")
check("a clean answer is returned and NOT logged as an error", body["code"] == 0 and not notes)
r = w.session_region()
check("session_region reads the IDC from the cookies", r == {"idc": "eu-ttp2", "signed": True, "store_idc": "eu-ttp2", "country": "lv", "host": "ads.tiktok.com"})

print("\n-- regional redirect --")
def h_redirect(req):
    seen.append(req)
    if req.url.host == "ads.tiktok.com":
        return httpx.Response(307, headers={"location": "https://ads-eu.tiktok.com" + req.url.raw_path.decode()})
    return httpx.Response(200, json={"code": 0, "msg": "OK", "data": {"host": req.url.host}})
fake(h_redirect); seen.clear(); notes.clear()
body = w.web_post("/api/v1/page/copy/", {"page_id": "p", "aadvid": "1", "target_aadvid": "2"})
check("a same-site non-login 307 is followed once, method + body kept, same cookies", len(seen) == 2 and seen[1].url.host == "ads-eu.tiktok.com" and seen[1].method == "POST"
      and json.loads(seen[1].content)["page_id"] == "p" and "tt-target-idc=eu-ttp2" in seen[1].headers.get("cookie", "") and body["data"]["host"] == "ads-eu.tiktok.com")
check("…and the hop is on Diagnostics", any(n[2] == "REDIRECT" and "ads-eu.tiktok.com" in n[3] for n in notes))

print("\n-- login redirect = expiry --")
def h_login(req):
    return httpx.Response(302, headers={"location": "https://ads.tiktok.com/i18n/login?redirect=x"})
fake(h_login); notes.clear()
try:
    w.web_get("/api/v1/page/list/", {"aadvid": "1"}); err = None
except w.WebAuthError as e:
    err = e
check("a redirect to login is still reported as expiry", err is not None and "Session expired" in str(err))

print("\n-- TikTok refusal --")
def h_refuse(req):
    return httpx.Response(200, json={"code": 40002, "msg": "target advertiser not in the same region", "data": {}})
fake(h_refuse); notes.clear()
body = w.web_post("/api/v1/page/copy/", {"page_id": "p", "aadvid": "1", "target_aadvid": "2"})
check("a non-zero code comes back to the caller (not an exception)", body["code"] == 40002)
n = next((x for x in notes if x[2] == 40002), None)
check("…and is logged with host + idc so a failed clone is a fact", n is not None and n[4]["host"] == "ads.tiktok.com" and n[4]["idc"] == "eu-ttp2" and "same region" in n[3])

print("\n-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
ip = read("app/routes/instant_pages.py"); lf = read("app/routes/lead_forms.py")
check("instant page clone reports TikTok's refusal (single + BC job)", "Clone to {label} failed" in ip and 'failed.append(f"{label}: {r[\'error\'][:140]}")' in ip)
check("lead form clone checks the answer and verifies by re-reading", "TikTok refused the copy" in lf and "no form with this name appeared" in lf)
check("host is a setting, default the global host", 'TIKTOK_ADS_WEB_HOST = (os.environ.get("TIKTOK_ADS_WEB_HOST") or "ads.tiktok.com")' in read("app/config.py") and 'ADS_BASE = "https://" + config.TIKTOK_ADS_WEB_HOST' in read("app/spark_web_api.py"))
check("Cookies page shows the session's data centre", "Region routing:" in read("app/templates/cookies_admin.html") and '"region": spark_web_api.session_region(stored)' in read("app/routes/cookies_admin.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
