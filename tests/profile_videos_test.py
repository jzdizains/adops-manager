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
# what /identity/video/get/ items look like (TikTok's ItemInfo): the cover and the playable
# preview live in video_info; a photo post carries carousel_info.image_info instead
VIDS = {"P1": [{"item_id": str(7000 + i), "text": f"post {i}", "item_type": "VIDEO" if i % 2 else "CAROUSEL", "auth_code": "", "create_time": "2026-09-0%d 10:00:00" % (i % 9 + 1),
                "video_info": ({"poster_url": f"https://cov/{i}", "preview_url": f"https://play/{i}", "duration": 12} if i % 2 else {}),
                "carousel_info": ({} if i % 2 else {"image_info": [{"image_url": f"https://img/{i}/1"}, {"image_url": f"https://img/{i}/2"}]})} for i in range(60)],
        "P2": [{"item_info": {"item_id": "8001", "text": "only one", "poster_url": "https://cov/x", "share_url": "https://www.tiktok.com/@creator.two/video/8001"}}]}
def list_tt_videos(token, adv, identity_id, identity_type, page=1, page_size=50, identity_authorized_bc_id="", **kw):
    calls.append(("videos", adv, identity_id, identity_type, identity_authorized_bc_id))
    return {"list": list(VIDS.get(identity_id, [])), "_keys": []}       # tiktok_api pages by cursor inside; the caller gets everything
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
check("one posts read per profile, under the BC (paging is tiktok_api's job)", [c for c in calls if c[0] == "videos"] == [("videos", "A1", "P1", "BC_AUTH_TT", "BC1"), ("videos", "A1", "P2", "BC_AUTH_TT", "BC1")])
p1 = r["profiles"][0]
check("every post listed, most-posts profile first, blank identities dropped", len(r["profiles"]) == 2 and p1["name"] == "creator.one" and len(p1["videos"]) == 60 and r["total"] == 61)
v = p1["videos"][1]
check("a video post: cover + playable preview + duration from video_info, url from the handle, created", v == {"item_id": "7001", "text": "post 1", "cover": "https://cov/1", "preview": "https://play/1", "slides": 0, "type": "video", "auth_code": "", "url": "https://www.tiktok.com/@creator.one/video/7001", "created": "2026-09-02 10:00:00", "duration": 12}, str(v))
check("a photo post: cover = its first image, slide count; share_url and flat poster_url still understood", p1["videos"][0]["type"] == "carousel" and p1["videos"][0]["cover"] == "https://img/0/1" and p1["videos"][0]["slides"] == 2 and r["profiles"][1]["videos"][0]["url"] == "https://www.tiktok.com/@creator.two/video/8001" and r["profiles"][1]["videos"][0]["cover"] == "https://cov/x")
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
picked = [{"item_id": "7002", "handle": "creator.one", "text": "post 2", "cover": "https://cov/2", "type": "carousel", "url": "https://t/2", "auth_code": "", "identity_id": "P1", "bc_id": "BC1"},
          {"item_id": "7001", "handle": "creator.one", "text": "post 1"}, {"item_id": "7002"}, {"item_id": ""}, {"item_id": "9999", "text": "x" * 200, "type": "video"}]
rows = pv.ensure_spark_rows(db, sc, picked)
check("one row per picked post in picked order; an existing row (by item id) is reused, duplicates and blanks dropped",
      [r.tiktok_item_id for r in rows] == ["7002", "7001", "9999"] and rows[1].name == "already here" and len(db.sparks) == 3 and db.committed >= 1)
new = rows[0]
check("a new row carries the post: no auth code, media type, no expiring cover, url, workspace owner, creator group, 80-char name",
      new.code == "" and new.media_type == "CAROUSEL" and new.thumbnail_url == "" and new.tiktok_post_url == "https://t/2" and new.owner_user_id == 7 and new.group_id == Group("creator.one").id and rows[2].name == "x" * 80 and rows[2].media_type == "VIDEO")

check("the row remembers the profile it came from (identity + its Business Center)", new.identity_id == "P1" and new.identity_bc_id == "BC1")

print("\n-- assignment --")
accts = [f"acct{i}" for i in range(5)]
pairs = pv.assign(accts, rows, 2)
check("each post covers N accounts in order; accounts past the picks get None", [(a, s) for a, s in pairs] == [("acct0", rows[0].id), ("acct1", rows[0].id), ("acct2", rows[1].id), ("acct3", rows[1].id), ("acct4", rows[2].id)] and pv.assign(accts + ["acct5", "acct6"], rows, 2)[-1][1] is None)

print("\n-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
sl = read("app/routes/super_launcher.py"); ca = read("app/routes/campaigns.py"); jh = read("app/job_handlers.py"); th = read("app/templates/super_launcher.html"); pj = read("app/static/picker.js")
check("feed route: per BC, in view only, cache + refresh", '@router.get("/super-launcher/profile-videos.json")' in sl and "profile_videos.list_for_bc(db, sc, bc_id, refresh=request.query_params.get(\"refresh\") == \"1\")" in sl and "has no account in this view" in sl)
check("launch: profile mode → spark rows → accounts capped to the picks → spark_pairs job", 'if creative_mode == "profile":' in sl and "profile_videos.ensure_spark_rows(db, sc, items)" in sl and "accounts = accounts[:len(profile_sparks) * per_creative]" in sl and "spark_pairs=[[a.advertiser_id, sid] for a, sid in pairs]" in sl and "Pick+at+least+one+profile+video" in sl)
check("resolve_spark: a profile post is checked against ITS profile under ITS Business Center first, BC identities before code identities after that",
      'if getattr(spark, "identity_id", "") and spark.tiktok_item_id:' in ca and '"_bc": spark.identity_bc_id or acct.owner_bc_id or ""' in ca and 'sorted(identities, key=lambda i: 0 if i.get("identity_type") == "BC_AUTH_TT" else 1)' in ca)
check("retry of a profile-video batch relaunches the SAME post on the same account (recipe keeps the per-account map)",
      '"_spark_by_account": {str(a.advertiser_id): int(sid) for a, sid in pairs if sid is not None}' in ca and 'by_acct = fields.pop("_spark_by_account", None) or {}' in ca and "pairs=lib_pairs or None, spark_pairs=pairs or None)" in ca)
check("SparkCode has the profile columns (auto-migrated at boot)", "identity_id = Column(String, default=\"\")" in read("app/models.py").split("class SparkCode(Base)")[1][:1500] and "identity_bc_id = Column(String, default=\"\")" in read("app/models.py"))
check("engine: spark-pairs runner (spark path, fixed text) + queue_launch carries spark_pairs; job handler routes them", "def run_batch_assigned_sparks" in ca and 'fields["creative_source"] = "spark"' in ca.split("def run_batch_assigned_sparks")[1][:1600] and '"spark_pairs": spark_pairs' in ca and 'if spark_pairs:' in jh and "engine.run_batch_assigned_sparks(db, spark_pairs, fields, batch_ref=ref," in jh)
check("page: profile posts are picked onto the board (v123: one 'Choose myself…' mode, hidden items field), BCs for the picker, validation", 'data-mode="items"' in th and 'name="items"' in th and "UI.pickProfileVideos({ bcs: BCS" in th and 'data-src="profile"' in th and "Pick at least one creative" in th and "bcs_json" in sl)
check("picker: PROFILE filter (not BC), Business Centers preloaded in the background & fetched several at a time, search, refresh, per-profile select all, playable preview, tidy footer",
      "UI.pickProfileVideos = function" in pj and 'class="pv-profile"' in pj and 'pv-bc' not in pj and "pvPump(" in pj and "UI.warmProfileVideos = function" in pj and 'pv-refresh' in pj and 'pv-all' in pj and "/super-launcher/profile-videos.json?bc_id=" in pj and "<video src=" in pj and 'open post ↗' in pj and ".pv-foot" in read("app/static/style.css"))
ap = read("app/static/account-picker.js"); apt = read("app/templates/_account_picker.html")
check("accounts step: ↻ Refresh re-reads statuses in place (route + JS + button), picks preserved", 'id="slRefresh"' in apt and 'fetch("/super-launcher/refresh-accounts"' in ap and "function applyStates(info, counts)" in ap and '@router.post("/super-launcher/refresh-accounts")' in sl and "tiktok_api.get_advertiser_info(tok, ids[i:i + 100])" in sl)
tk = read("app/tiktok_api.py")
check("/identity/video/get/ is paged by cursor+count (≤20) until has_more is false, for VIDEO and CAROUSEL posts", '"cursor": cursor, "count": IDENTITY_VIDEO_COUNT' in tk and 'if not data.get("has_more")' in tk and 'item_types: tuple = ("VIDEO", "CAROUSEL")' in tk and "IDENTITY_VIDEO_COUNT = 20" in tk)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
