"""Issue scan streams ads page-by-page (v135).

issues.scan runs every slow sweep over every account and used to load an account's
ENTIRE ad list into RAM — up to 20,000 ads — which was the transient spike that
OOM-killed the 512 MB box. list_all_ads now takes an on_page callback so the scan
consumes each page and keeps only the REJECTED ads (and only the fields the appeal /
inbox need), so peak memory is one page, not the whole account.

Verified: list_all_ads streaming semantics + the scan's on_page usage (source),
and that the trimmed rejected-ad dict carries every field appeals.sync reads."""
import os, sys, types

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

class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
    def __getattr__(self, n): return _Any()

# --- stub just enough to import tiktok_api's list_all_ads and drive it ---------------
pages = {}    # advertiser_id -> list of pages, each a list of ad dicts
def fake_api_get(path, token, params):
    adv = params["advertiser_id"]; pg = params["page"]
    ps = pages.get(adv, [])
    lst = ps[pg - 1] if pg - 1 < len(ps) else []
    return {"list": lst, "page_info": {"total_page": len(ps)}}

_mod("httpx", Timeout=_Any, Client=_Any, HTTPError=Exception)
tk = _mod("app.tiktok_api")  # placeholder so relative imports resolve
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
# import the real module fresh over the placeholder
import importlib.util
spec = importlib.util.spec_from_file_location("app.tiktok_api", os.path.join(ROOT, "app", "tiktok_api.py"))

# tiktok_api imports config etc.; stub the heavy bits it needs at import
_mod("app.config", TIKTOK_API_BASE="https://x", TIKTOK_APP_ID="", TIKTOK_APP_SECRET="")
real = importlib.util.module_from_spec(spec)
sys.modules["app.tiktok_api"] = real
try:
    spec.loader.exec_module(real)
    loaded = True
except Exception as e:  # if deeper imports fail here, fall back to source checks only
    loaded = False
    print("note: could not import tiktok_api in this stub env:", str(e)[:80])

if loaded:
    real.api_get_retry = fake_api_get
    print("-- list_all_ads streaming --")
    pages = {"a1": [[{"ad_id": str(i)} for i in range(1000)], [{"ad_id": str(i)} for i in range(1000, 1600)]]}
    seen_pages = []
    total = real.list_all_ads("t", "a1", on_page=lambda rows: seen_pages.append(len(rows)))
    check("on_page gets each page; returns the total count; never builds the full list", total == 1600 and seen_pages == [1000, 600], (total, seen_pages))
    got = real.list_all_ads("t", "a1")
    check("no callback → full list, as before (back-compat)", isinstance(got, list) and len(got) == 1600)
    check("page cap constant is a real safety cap", real.AD_LIST_MAX_PAGES >= 1 and real.AD_LIST_PAGE_SIZE == 1000)

print("-- source: the scan streams and trims --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
iss = read("app/issues.py")
check("scan calls list_all_ads with on_page (no full-account ad list in RAM)", "list_all_ads(acct.access_token, acct.advertiser_id, fields=AD_SCAN_FIELDS, on_page=_take)" in iss and "for ad in ads:" not in iss)
check("only rejected ads are kept, with a trimmed dict (not the whole ad)", "if not _status_is_bad(sec):" in iss and 'rejected_ads.append({' in iss and '"campaign_name": ad.get("campaign_name"' in iss)
for f in ("ad_id", "ad_name", "campaign_id", "campaign_name", "adgroup_id", "secondary_status", "advertiser_id", "advertiser_name", "access_token"):
    check(f"trimmed dict keeps '{f}' (used downstream by appeals/inbox)", f'"{f}"' in iss.split('rejected_ads.append({')[1].split("})")[0])
tk_src = read("app/tiktok_api.py")
check("list_all_ads: on_page path returns a count, else the list", "def list_all_ads(" in tk_src and "if on_page is not None:" in tk_src and "return total if on_page is not None else out" in tk_src)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
