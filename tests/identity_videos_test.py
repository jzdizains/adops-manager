"""tiktok_api.list_tt_videos — every post of an identity (v121).

/identity/video/get/ pages with cursor + count (count ≤ 20) and answers has_more; page /
page_size are not its parameters. With them alone only the first 20 posts ever came back
(the picker showed "20 posts" for a profile with more, and a profile's 21st post could
never be resolved for a launch). item_type defaults to VIDEO, so photo posts need their
own read. Against a stubbed api_get:
- follows the cursor until has_more is false, count 20, no page/page_size sent
- asks VIDEO then CAROUSEL, merges, dedupes by item id
- a refused CAROUSEL read doesn't fail the VIDEO posts; a refused first read raises
- the BC id travels on every call; the page cap holds"""
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

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
_mod("app.config", TIKTOK_API_BASE="https://x", TIKTOK_APP_ID="", TIKTOK_APP_SECRET="", OAUTH_REDIRECT_URI="")
import importlib
try:
    tk = importlib.import_module("app.tiktok_api")
except ModuleNotFoundError as e:
    print("SKIP:", e); sys.exit(0)

calls = []
POSTS = {"VIDEO": [{"item_id": str(100 + i), "item_type": "VIDEO"} for i in range(45)],
         "CAROUSEL": [{"item_id": "900", "item_type": "CAROUSEL"}, {"item_id": "101", "item_type": "CAROUSEL"}]}   # 101 also listed as a video → deduped
refuse_carousel = {"on": False}
def fake_get(path, token, params):
    calls.append((path, dict(params)))
    if refuse_carousel["on"] and params["item_type"] == "CAROUSEL":
        raise tk.TikTokError("40002", "item_type not supported")
    items = POSTS[params["item_type"]]
    start = int(params.get("cursor") or 0); count = int(params["count"])
    batch = items[start:start + count]
    return {"video_list": batch, "cursor": start + len(batch), "has_more": start + len(batch) < len(items)}
tk.api_get = fake_get

r = tk.list_tt_videos("t", "adv1", "ident1", "BC_AUTH_TT", identity_authorized_bc_id="bc9")
ids = [x["item_id"] for x in r["list"]]
check("every post comes back: 45 videos + 1 extra photo post, deduped, in order", len(ids) == 46 and ids[:3] == ["100", "101", "102"] and ids[-1] == "900", str(len(ids)))
vid = [c for c in calls if c[1]["item_type"] == "VIDEO"]
check("videos: 3 cursor pages of 20 (0, 20, 40), no page/page_size, BC id on every call",
      [c[1]["cursor"] for c in vid] == [0, 20, 40] and all(c[1]["count"] == 20 and "page" not in c[1] and "page_size" not in c[1] and c[1]["identity_authorized_bc_id"] == "bc9" for c in vid), str([c[1] for c in vid]))
check("then one CAROUSEL read", [c[1]["item_type"] for c in calls] == ["VIDEO", "VIDEO", "VIDEO", "CAROUSEL"])

calls.clear(); refuse_carousel["on"] = True
r = tk.list_tt_videos("t", "adv1", "ident1", "TT_USER")
check("a refused photo-post read still returns the videos", len(r["list"]) == 45)
refuse_carousel["on"] = False

def refuse_all(path, token, params): raise tk.TikTokError("40002", "no")
tk.api_get = refuse_all
try:
    tk.list_tt_videos("t", "adv1", "ident1", "TT_USER"); raised = False
except tk.TikTokError:
    raised = True
check("a refused first read raises (the caller decides)", raised)

def endless(path, token, params):
    return {"video_list": [{"item_id": str(params.get("cursor") or 0) + "x"}], "cursor": int(params.get("cursor") or 0) + 1, "has_more": True}
tk.api_get = endless
r = tk.list_tt_videos("t", "adv1", "ident1", "TT_USER", item_types=("VIDEO",))
check("the page cap holds when TikTok keeps saying has_more", len(r["list"]) == tk.IDENTITY_VIDEO_MAX_PAGES)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
