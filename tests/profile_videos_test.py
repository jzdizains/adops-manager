"""Super Launcher › Profile videos (v120): every post of every profile a Business Center
shares, picked straight from the launcher, launched as spark posts spread over accounts.

Functional (stubbed TikTok + a tiny in-memory db):
- fetch_bc asks /identity/get/ (BC_AUTH_TT + the BC id) through ONE enabled account of
  the BC, then /identity/video/get/ per profile, paging until a short page; every post
  is listed with cover, caption, type, url; profiles with most posts first
- list_for_bc caches per (workspace, BC) and refresh re-reads
- ensure_spark_rows: one SparkCode per picked post, found by item id, made once (no
  auth code, media type from the post, grouped under the handle), picked order kept
- assign: each post covers N accounts in order
Static: feed route, launch branch (profile → spark rows → spark_pairs job), runner,
job handler, page + picker UI."""
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
_mod("sqlalchemy.orm", Session=object)

class AdAccount:
    def __init__(self, advertiser_id, name, bc, token="t", enabled=True):
        self.advertiser_id, self.advertiser_name, self.owner_bc_id, self.access_token, self.enabled = advertiser_id, name, bc, token, enabled
    enabled = True
    owner_bc_id = None
    advertiser_name = None
class SparkCode:
    _n = 0
    tiktok_item_id = None
    def __init__(self, **kw):
        SparkCode._n += 1; self.id = SparkCode._n
        for k, v in kw.items(): setattr(self, k, v)
class Group:
    def __init__(self, name): self.id, self.name = hash(name) % 1000, name
_mod("app.models", AdAccount=AdAccount, SparkCode=SparkCode)
_mod("app.routes"); sys.modules["app.routes"].__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.routes.spark_codes", _group_in_view=lambda db, sc, name: Group(name))

# ---- fake TikTok -----------------------------------------------------------------
calls = []
class TikTokError(Exception):
    def __init__(self, code, message): super().__init__(message); self.code, self.message = code, message
def list_identities(token, adv, identity_type=None, identity_authorized_bc_id=""):
    calls.append(("identities", adv, identity_type, identity_authorized_bc_id))
    if identity_authorized_bc_id == "BC1":
        return [{"identity_id": "P1", "display_name": "creator.one", "profile_image": "https://img/p1"},
                {"identity_id": "P2", "display_name": "creator.two"}, {"identity_id": ""}]
    return []
VIDS = {"P1": [{"item_info": {"item_id": str(7000 + i), "text": f"post {i}", "video_cover_url": f"https://cov/{i}", "item_type": "VIDEO" if i % 2 else "CAROUSEL", "auth_code": "", "create_time": "2026-09-0%d 10:00:00" % (i % 9 + 1)}} for i in range(60)],
        "P2": [{"item_info": {"item_id": "8001", "text": "only one", "poster_url": "https://cov/x", "share_url": "https://www.tiktok.com/@creator.two/video/8001"}}]}
def list_tt_videos(token, adv, identity_id, identity_type, page=1, page_size=50, identity_authorized_bc_id=""):
    calls.append(("videos", adv, identity_id, identity_type, page, identity_authorized_bc_id))
    items = VIDS.get(identity_id, [])[(page - 1) * page_size: page * page_size]
    return {"list": items, "_keys": []}
_mod("app.tiktok_api", TikTokError=TikTokError, list_identities=list_identities, list_tt_videos=list_tt_videos)

import importlib
pv = importlib.import_module("app.profile_videos")

# ---- tiny db ---------------------------------------------------------------------
class Q:
    def __init__(self, rows): self.rows = list(rows)
    def filter(self, *conds):
        out = self.rows
        for c in conds:
            out = [r for r in out if c(r)]
        return Q(out)
    def filter_by(self, **kw): return Q([r for r in self.rows if all(getattr(r, k, None) == v for k, v in kw.items())])
    def order_by(self, *a): return self
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows
    def __iter__(self): return iter(self.rows)
class Col:
    def __init__(self, name): self.name = name
    def __eq__(self, other): return lambda r: getattr(r, self.name, None) == other
AdAccount.enabled = Col("enabled"); AdAccount.owner_bc_id = Col("owner_bc_id"); AdAccount.advertiser_name = Col("advertiser_name")
SparkCode.tiktok_item_id = Col("tiktok_item_id"); SparkCode.id = Col("id")
class DB:
    def __init__(self): self.accounts, self.sparks, self.committed = [], [], 0
    def query(self, model):
        return Q(self.accounts if model is AdAccount else self.sparks)
    def add(self, row): self.sparks.append(row)
    def flush(self): pass
    def commit(self): self.committed += 1
class SC:
    user_id, owner_for_new = 7, 7
    def allows(self, adv): return adv != "A-other"
    def owned(self, q, model): return q

db = DB(); sc = SC()
db.accounts = [AdAccount("A-other", "other", "BC1"), AdAccount("A1", "blue bat", "BC1"), AdAccount("A2", "bat 2", "BC1", enabled=False), AdAccount("B1", "x", "BC2")]
# the Col trick: make constructor-set attributes win over the class-level Col objects
for a in db.accounts:
    a.__dict__.setdefault("enabled", True)

print("\n-- fetch --")
calls.clear()
r = pv.fetch_bc(db, sc, "BC1")
check("asks through one enabled, in-view account of the BC with the BC id and BC_AUTH_TT", r["ok"] and r["via"] == "A1" and calls[0] == ("identities", "A1", "BC_AUTH_TT", "BC1"), str(calls[:2]))
check("pages each profile until a short page (60 posts = 2 pages, 1 post = 1 page)", [c for c in calls if c[0] == "videos" and c[2] == "P1"] == [("videos", "A1", "P1", "BC_AUTH_TT", 1, "BC1"), ("videos", "A1", "P1", "BC_AUTH_TT", 2, "BC1")] and len([c for c in calls if c[0] == "videos" and c[2] == "P2"]) == 1)
p1 = r["profiles"][0]
check("every post listed, most-posts profile first, blank identities dropped", len(r["profiles"]) == 2 and p1["name"] == "creator.one" and len(p1["videos"]) == 60 and r["total"] == 61)
v = p1["videos"][1]
check("post fields: item id, caption, cover, type, url from the handle, created", v == {"item_id": "7001", "text": "post 1", "cover": "https://cov/1", "type": "video", "auth_code": "", "url": "https://www.tiktok.com/@creator.one/video/7001", "created": "2026-09-02 10:00:00", "duration": 0}, str(v))
check("carousel posts are marked, share_url wins when TikTok gives one", p1["videos"][0]["type"] == "carousel" and r["profiles"][1]["videos"][0]["url"] == "https://www.tiktok.com/@creator.two/video/8001" and r["profiles"][1]["videos"][0]["cover"] == "https://cov/x")
check("a BC with no usable account says so", pv.fetch_bc(db, sc, "BC9")["ok"] is False and "No enabled, connected ad account" in pv.fetch_bc(db, sc, "BC9")["error"])

print("\n-- cache --")
calls.clear(); pv._CACHE.clear()
a = pv.list_for_bc(db, sc, "BC1"); n1 = len(calls)
b = pv.list_for_bc(db, sc, "BC1"); n2 = len(calls)
c = pv.list_for_bc(db, sc, "BC1", refresh=True); n3 = len(calls)
check("second read is served from the cache; refresh re-reads", n1 > 0 and n2 == n1 and n3 == 2 * n1 and a["cached"] is False and b["cached"] is True and c["cached"] is False)
check("cache is per workspace and bounded", list(pv._CACHE)[0] == "7:BC1" and pv.CACHE_MAX == 30 and pv.CACHE_S == 600)

print("\n-- spark rows --")
db.sparks = [SparkCode(tiktok_item_id="7001", name="already here", code="", media_type="VIDEO")]
picked = [{"item_id": "7002", "handle": "creator.one", "text": "post 2", "cover": "https://cov/2", "type": "carousel", "url": "https://t/2", "auth_code": ""},
          {"item_id": "7001", "handle": "creator.one", "text": "post 1"}, {"item_id": "7002"}, {"item_id": ""}, {"item_id": "9999", "text": "x" * 200, "type": "video"}]
rows = pv.ensure_spark_rows(db, sc, picked)
check("one row per picked post in picked order; an existing row (by item id) is reused, duplicates and blanks dropped",
      [r.tiktok_item_id for r in rows] == ["7002", "7001", "9999"] and rows[1].name == "already here" and len(db.sparks) == 3 and db.committed >= 1)
new = rows[0]
check("a new row carries the post: no auth code, media type, cover, url, workspace owner, creator group, 80-char name",
      new.code == "" and new.media_type == "CAROUSEL" and new.thumbnail_url == "https://cov/2" and new.tiktok_post_url == "https://t/2" and new.owner_user_id == 7 and new.group_id == Group("creator.one").id and rows[2].name == "x" * 80 and rows[2].media_type == "VIDEO")

print("\n-- assignment --")
accts = [f"acct{i}" for i in range(5)]
pairs = pv.assign(accts, rows, 2)
check("each post covers N accounts in order; accounts past the picks get None", [(a, s) for a, s in pairs] == [("acct0", rows[0].id), ("acct1", rows[0].id), ("acct2", rows[1].id), ("acct3", rows[1].id), ("acct4", rows[2].id)] and pv.assign(accts + ["acct5", "acct6"], rows, 2)[-1][1] is None)

print("\n-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
sl = read("app/routes/super_launcher.py"); ca = read("app/routes/campaigns.py"); jh = read("app/job_handlers.py"); th = read("app/templates/super_launcher.html"); pj = read("app/static/picker.js")
check("feed route: per BC, in view only, cache + refresh", '@router.get("/super-launcher/profile-videos.json")' in sl and "profile_videos.list_for_bc(db, sc, bc_id, refresh=request.query_params.get(\"refresh\") == \"1\")" in sl and "has no account in this view" in sl)
check("launch: profile mode → spark rows → accounts capped to the picks → spark_pairs job", 'if creative_mode == "profile":' in sl and "profile_videos.ensure_spark_rows(db, sc, items)" in sl and "accounts = accounts[:len(profile_sparks) * per_creative]" in sl and "spark_pairs=[[a.advertiser_id, sid] for a, sid in pairs]" in sl and "Pick+at+least+one+profile+video" in sl)
check("engine: spark-pairs runner (spark path, fixed text) + queue_launch carries spark_pairs; job handler routes them", "def run_batch_assigned_sparks" in ca and 'fields["creative_source"] = "spark"' in ca.split("def run_batch_assigned_sparks")[1][:900] and '"spark_pairs": spark_pairs' in ca and 'elif p.get("spark_pairs"):' in jh and "engine.run_batch_assigned_sparks(db, pairs, fields, batch_ref=ref, on_progress=prog)" in jh)
check("page: Profile videos… button, hidden items field, chips, BCs for the picker, summary + validation", 'data-mode="profile"' in th and 'name="profile_items"' in th and "UI.pickProfileVideos({ bcs: BCS" in th and 'profile — none picked yet' not in th and "Pick at least one profile video" in th and "bcs_json" in sl)
check("picker: BC select, search, refresh, per-profile select all, every post as a tile, multi-select, preview with post link", "UI.pickProfileVideos = function" in pj and 'class="pv-bc"' in pj and 'pv-refresh' in pj and 'pv-all' in pj and "/super-launcher/profile-videos.json?bc_id=" in pj and 'post ↗' in pj and ".pv-head" in read("app/static/style.css"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
