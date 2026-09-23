"""Phase 3 (v147) — stocking systems.

 11. Spark codes checked on paste: post id / type / link / cover filled in, a refused code
     marked at once, duplicates flagged, other failures retried at 30 s, 2 min, 5 min.
 12. Thumbnails we own: covers copied from TikTok's CDN while the signed URL is alive —
     only https TikTok hosts, size-capped, one re-checked redirect.
 13. Profile posts kept in SQLite: served at once (stale copies re-read in the background),
     covers saved right after each read.
 14. Instant Pages kept stocked per preset page, web API only, with guard rails.
 15. Display cards pushed to every account ahead of launch, with a status per account.

Pure modules run for real; DB pieces on small fakes; routes/templates source-asserted."""
import importlib, io, json, os, sys, tempfile, types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = tempfile.mkdtemp()
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

thumbs = importlib.import_module("app.thumbs")
sk = importlib.import_module("app.spark_check")
ps = importlib.import_module("app.page_stock")

# =======================================================================================
print("-- 12. thumbnails we own --")
check("TikTok CDN over https is fetched", thumbs.allowed("https://p16-sign-va.tiktokcdn.com/obj/abc~tplv.jpeg?x=1"))
check("plain http, other hosts and look-alikes are not",
      not thumbs.allowed("http://p16.tiktokcdn.com/a.jpg") and not thumbs.allowed("https://evil.example/a.jpg")
      and not thumbs.allowed("https://tiktokcdn.com.evil.example/a.jpg") and not thumbs.allowed("https://127.0.0.1/a.jpg")
      and not thumbs.allowed("javascript:alert(1)") and not thumbs.allowed(""))
check("keys can't walk the filesystem", thumbs.path_for("sp", "../../etc/passwd") is None and thumbs.path_for("xx", "1") is None
      and thumbs.path_for("pp", "7412345678901234567").name == "pp_7412345678901234567.jpg")
from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (1080, 1920), (200, 30, 90)).save(buf, "PNG"); big = buf.getvalue()
small = thumbs.shrink(big)
im = Image.open(io.BytesIO(small))
check("covers are stored as a small JPEG", im.format == "JPEG" and max(im.size) == thumbs.PX and len(small) < len(big))
import httpx
def transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
ok, why = thumbs.cache("pp", "111", "https://p16.tiktokcdn.com/a.jpg", transport(lambda r: httpx.Response(200, content=big)))
check("a live cover is copied", ok and thumbs.have("pp", "111") and "111" in thumbs.owned_keys("pp") or (thumbs.forget_owned() or "111" in thumbs.owned_keys("pp")))
ok, why = thumbs.cache("pp", "112", "https://p16.tiktokcdn.com/a.jpg", transport(lambda r: httpx.Response(403)))
check("an expired URL just fails, quietly", not ok and "403" in why and not thumbs.have("pp", "112"))
ok, why = thumbs.cache("pp", "113", "https://p16.tiktokcdn.com/a.jpg",
                       transport(lambda r: httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data"})))
check("a redirect off TikTok's CDN is refused", not ok and "CDN" in why)
ok, why = thumbs.cache("pp", "114", "https://p16.tiktokcdn.com/a.jpg", transport(lambda r: httpx.Response(200, content=b"x" * (thumbs.MAX_BYTES + 10))))
check("oversized answers are cut off", not ok and "large" in why)
ok, why = thumbs.cache("pp", "115", "https://p16.tiktokcdn.com/a.jpg", transport(lambda r: httpx.Response(200, content=b"<html>not an image</html>")))
check("a non-image is refused", not ok)
spark = types.SimpleNamespace(id=7, thumbnail_url="https://p16.tiktokcdn.com/c.jpg")
check("a spark's cover, once owned, points at our route",
      thumbs.cache_spark(None, spark, client=transport(lambda r: httpx.Response(200, content=big))) and spark.thumbnail_url == "/thumbs/sp/7.jpg")
check("the background pass only picks TikTok's expiring URLs", 'thumbnail_url.like(f"https://%{sfx}/%")' in read("app/thumbs.py"))
sc_src = read("app/routes/spark_codes.py")
check("served from our route, spark covers only to their workspace, cached a week",
      '@router.get("/thumbs/{kind}/{key}.jpg")' in sc_src and "owns(row)" in sc_src and "max-age=604800" in sc_src)

# =======================================================================================
print("-- 11. spark codes checked on paste --")
check("classify: TikTok's 'expired' is the code's fault",
      sk.classify(40002, "The auth code has expired") == "bad" and sk.classify(40001, "code is invalid") == "bad")
check("classify: rate limit / not indexed / blips are worth another try",
      sk.classify(40100, "Too many requests") == "transient" and sk.classify("APP", "TikTok didn't return the post yet (not indexed)") == "transient"
      and sk.classify("HTTP", "boom") == "transient")
check("classify: anything else is an error, not a verdict on the code ('operate' isn't 'rate')",
      sk.classify(40001, "No permission to operate advertiser") == "error" and sk.classify(1, "Request rate exceeded") == "transient")
check("the ladder: 30 s, 2 min, 5 min, then give up",
      [sk.next_step(i, "transient") for i in (1, 2, 3, 4)] == [("checking", 30), ("checking", 120), ("checking", 300), ("error", None)]
      and sk.next_step(1, "bad") == ("bad", None))
f = sk.post_facts({"item_id": "741"}, {"list": [{"item_info": {"item_type": "CAROUSEL", "text": "hi"}, "video_info": {"poster_url": "https://p16.tiktokcdn.com/p.jpg"}, "share_url": "https://www.tiktok.com/@a/photo/741"}]})
check("post facts from the mixed answer shapes", f == {"item_id": "741", "media_type": "CAROUSEL", "cover": "https://p16.tiktokcdn.com/p.jpg",
                                                         "url": "https://www.tiktok.com/@a/photo/741", "caption": "hi"}, f)

class TikTokError(Exception):
    def __init__(self, code, message, *a):
        super().__init__(message); self.code, self.message = code, message
class Api:
    TikTokError = TikTokError
    def __init__(self, authz=None, info=None, authz_err=None, info_err=None):
        self.authz, self.info, self.authz_err, self.info_err, self.calls = authz, info, authz_err, info_err, []
    def authorize_tt_video(self, tok, adv, code):
        self.calls.append(("authorize", adv, code))
        if self.authz_err: raise self.authz_err
        return self.authz or {}
    def tt_video_info(self, tok, adv, auth_code=""):
        self.calls.append(("info", adv, auth_code))
        if self.info_err: raise self.info_err
        return self.info or {}
class Col:
    def __init__(self, n): self.n = n
    def __eq__(self, v): return lambda r: getattr(r, self.n) == v
    def __ne__(self, v): return lambda r: getattr(r, self.n) != v
    def __hash__(self): return hash(self.n)
class Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *c): return Q([r for r in self.rows if all(f(r) for f in c)])
    def order_by(self, *a): return self
    def first(self): return self.rows[0] if self.rows else None
class Spark(types.SimpleNamespace):
    pass
Spark.tiktok_item_id, Spark.id, Spark.owner_user_id = Col("tiktok_item_id"), Col("id"), Col("owner_user_id")
M = types.SimpleNamespace(SparkCode=Spark)
def mk(**kw):
    base = dict(id=1, code="CT7QABCDEFGHIJ", name="CT7QABCDEFGH", owner_user_id=3, tiktok_item_id="", media_type="VIDEO",
                tiktok_post_url="", thumbnail_url="", check_state="checking", check_error="", check_attempts=0,
                check_next_at=None, checked_at=None)
    base.update(kw); return Spark(**base)
class DB:
    def __init__(self, rows): self.rows = rows
    def query(self, m): return Q(self.rows)
anchor = types.SimpleNamespace(advertiser_id="A1", access_token="t", status="STATUS_ENABLE")

s = mk()
api = Api(authz={"item_id": "7412"}, info={"item_info": {"item_type": "VIDEO", "text": "My post"}, "share_url": "https://www.tiktok.com/@a/video/7412"})
check("a good code: authorised on the anchor, post filled in", sk.check_one(DB([s]), M, s, api, anchor) == "ok"
      and (s.tiktok_item_id, s.media_type, s.tiktok_post_url, s.name) == ("7412", "VIDEO", "https://www.tiktok.com/@a/video/7412", "My post")
      and api.calls[0] == ("authorize", "A1", "CT7QABCDEFGHIJ") and s.check_error == "")
s2 = mk(id=2)
check("TikTok's refusal marks it bad at once, in TikTok's words, without a lookup",
      sk.check_one(DB([s2]), M, s2, Api(authz_err=TikTokError(40002, "The auth code has expired")), anchor) == "bad"
      and "expired" in s2.check_error and s2.check_next_at is None)
s3 = mk(id=3)
st = sk.check_one(DB([s3]), M, s3, Api(authz={}, info={}), anchor)
check("TikTok hasn't indexed the post yet → retried in 30 s", st == "checking" and 25 <= (s3.check_next_at - datetime.utcnow()).total_seconds() <= 31)
for _ in range(3):
    st = sk.check_one(DB([s3]), M, s3, Api(authz={}, info={}), anchor)
check("…and after the ladder it's 'couldn't check', not a verdict", st == "error" and s3.check_attempts == 4)
s4 = mk(id=4)
check("an 'already authorised' error still reads the post",
      sk.check_one(DB([s4]), M, s4, Api(authz_err=TikTokError(40002, "already authorized"), info={"item_id": "999"}), anchor) == "ok" and s4.tiktok_item_id == "999")
first = mk(id=5, tiktok_item_id="555", name="Original", check_state="ok")
s6 = mk(id=6)
sk.check_one(DB([first, s6]), M, s6, Api(authz={"item_id": "555"}), anchor)
check("the same post pasted twice is flagged against the first", s6.check_state == "ok" and "Original" in s6.check_error and "#5" in s6.check_error)
other_ws = mk(id=8, tiktok_item_id="777", owner_user_id=9)
s7 = mk(id=7)
sk.check_one(DB([other_ws, s7]), M, s7, Api(authz={"item_id": "777"}), anchor)
check("…but only within the same workspace", s7.check_error == "")
s9 = mk(id=9, code="", tiktok_item_id="1234")
check("a profile pick (no code) needs no check", sk.check_one(DB([s9]), M, s9, Api(), anchor) == "ok")
spc = read("app/spark_check.py")
check("the claim is committed before any network call, and the cover download can't hold a write lock",
      "r.check_next_at = now + timedelta(seconds=CLAIM_S)" in spc and "no_autoflush" in spc)
check("new codes are marked and checked in the background (single + bulk add); grabbed/profile rows need no check",
      sc_src.count("spark_check.kick(db,") >= 3 and sc_src.count('check_state="checking"') == 2 and 'check_state="ok"' in sc_src
      and 'check_state="ok"' in read("app/profile_videos.py"))
check("pickers and the Sparks table show the check", '"check": {"state": s.check_state' in sc_src
      and read("app/static/picker.js").count("ckChip(it.check)") == 2 and "✕ code rejected" in read("app/templates/spark_codes.html"))
md = read("app/models.py")
check("SparkCode check columns; old codes stay unchecked (default '')",
      'check_state = Column(String, default="", index=True)' in md and "check_next_at = Column(DateTime" in md)

# =======================================================================================
print("-- 13. profile posts in SQLite --")
pv = read("app/profile_videos.py")
check("memory → database → TikTok; a stale copy is served at once and re-read in the background",
      "stored = load_stored(db, bc_id)" in pv and "schedule_refresh(db, bc_id" in pv and '"stale": True' in pv)
check("a fresh read replaces the BC's stored posts and queues the cover copy",
      "store(db, out)" in pv and 'jobs.enqueue(db, "post_thumbs"' in pv and ".delete(synchronize_session=False)" in pv)
check("stored covers become ours when we have them", '"/thumbs/pp/%s.jpg" % r.item_id' in pv and "thumbs.owned_keys(\"pp\")" in pv)
check("one refresh per BC at a time (payload parsed, not substring-matched)", "_bc(j) == str(bc_id)" in pv)
check("ProfileFetch / ProfilePost tables", "class ProfileFetch(Base):" in md and "class ProfilePost(Base):" in md and 'UniqueConstraint("bc_id", "identity_id", "item_id"' in md)
check("a pick keeps its fresh cover so the background pass can own it", "thumbs.allowed(cov)" in pv)

# =======================================================================================
print("-- 14. Instant Pages kept stocked --")
blobs = [json.dumps({"destination_type": "instant_page", "instant_page_name": "Offer A"}), json.dumps({"destination_type": "website"}),
         json.dumps({"destination_type": "instant_page", "instant_page_name": "Offer B"}), json.dumps({"destination_type": "instant_page", "instant_page_name": "Offer A"}), "not json"]
check("offers = the page names presets launch to", ps.offer_names(blobs) == ["Offer A", "Offer B"])
check("missing pairs spread each page over the accounts", ps.missing_pairs(["A", "B"], ["1", "2"], {("1", "A")}) == [("2", "A"), ("1", "B"), ("2", "B")])
st = ps.after_result({}, False, "x", "boom")
check("one failure keeps going", st["streak"] == 1 and not st.get("paused"))
st = ps.after_result(st, False, "y", "boom again")
check("two failures in a row pause it, saying why", st["streak"] == 2 and "paused" in st["paused"] and "boom again" in st["paused"])
st = ps.after_result({"streak": 1}, True, "z")
check("a success resets the streak and counts", st["streak"] == 0 and st["made"] == 1)
pss = read("app/page_stock.py")
check("web API only — never the browser builder", "ip.clone_one(" in pss and "instant_page_builder" not in pss.split('"""', 2)[2])
check("dead cookies stop the run and pause it", "except spark_web_api.WebAuthError" in pss and "session is dead" in pss)
check("off by default, per workspace, at most 6 per run, every 15 min",
      'bool(st.get("on"))' in pss and "MAX_PER_RUN = 6" in pss and "RUN_EVERY = timedelta(minutes=15)" in pss)
check("copies come only from this workspace's own published pages", "if owner in ids:" in pss and '== "PUBLISHED"' in pss)
ipr = read("app/routes/instant_pages.py"); ipt = read("app/templates/instant_pages.html")
check("Instant Pages: a 'Keep stocked' card with coverage per page and a confirm to turn on",
      '@router.post("/instant-pages/stock")' in ipr and 'id="stocking"' in ipt and "Keep Instant Pages stocked?" in ipt
      and '<input type="hidden" name="action" value="on">' in ipt)

# =======================================================================================
print("-- 15. display cards pushed ahead of launch --")
dc = read("app/display_cards.py")
ns = {"models": types.SimpleNamespace(), "tiktok_api": types.SimpleNamespace(TikTokError=TikTokError)}
src = dc[dc.index("def push("):dc.index("def status(")]
exec("from __future__ import annotations\n" + src, ns)
class Up(types.SimpleNamespace): pass
class UQ:
    def __init__(self, rows): self.rows = rows
    def filter_by(self, **kw): return UQ([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])
    def first(self): return self.rows[0] if self.rows else None
    def __iter__(self): return iter(self.rows)
class UDB:
    def __init__(self): self.rows, self.commits, self.rollbacks = [], 0, 0
    def query(self, m): return UQ(self.rows)
    def add(self, r): self.rows.append(r)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
card = types.SimpleNamespace(id=4, md5="m1", name="Card")
udb = UDB(); udb.rows.append(Up(card_id=4, advertiser_id="A1", portfolio_id="P1", upload_md5="m1", status="ok", error=""))
made = []
def resolve(db, acct, c):
    if acct.advertiser_id == "A3":
        raise TikTokError(40002, "image rejected")
    made.append(acct.advertiser_id)
ns["resolve_for_account"] = resolve
ns["models"].DisplayCardUpload = lambda **kw: Up(**{"portfolio_id": "", "upload_md5": "", **kw})
accts = [types.SimpleNamespace(advertiser_id=a, advertiser_name=a) for a in ("A1", "A2", "A3")]
import time as _t; _sleep = _t.sleep; _t.sleep = lambda s: None
r = ns["push"](udb, card, accts)
_t.sleep = _sleep
check("push: accounts that have it are skipped, the rest get it, a refusal is recorded per account",
      r == {"done": 1, "skipped": 1, "failed": ["A3"]} and made == ["A2"]
      and any(x.advertiser_id == "A3" and x.status == "failed" and "image rejected" in x.error for x in udb.rows))
exec("from __future__ import annotations\n" + dc[dc.index("def status("):], ns)
st = ns["status"](UDB.__new__(UDB) if False else udb, card, accts)
check("status: ready / failed (with why) / still to add", st["ready"] == 1 and st["n_failed"] == 1 and st["failed"][0]["error"].startswith("40002") and st["missing"] == 1)
dcr = read("app/routes/display_cards.py")
check("routes: status.json + push; an upload is pushed right away; one push per card at a time",
      '/display-cards/{card_id}/status.json' in dcr and '/display-cards/{card_id}/push' in dcr and "enqueue_push(db, card, quiet=True)" in dcr
      and "_push_busy(db, card.id)" in dcr)
pb = read("app/static/preset-builder.js")
check("the preset's Add-ons step shows 'On N of M accounts' with a push button", "/status.json" in pb and "Put on every account" in read("app/templates/template_form.html") and 'dcStock()' in pb)
check("the launch-time path records success too", 'status="ok", updated_at=_dt.utcnow()' in dc)

# =======================================================================================
print("-- wiring --")
jh = read("app/job_handlers.py"); jb = read("app/jobs.py"); bg = read("app/background.py")
check("job handlers", all(f'@jobs.handler("{k}")' in jh for k in ("spark_check", "post_thumbs", "profile_refresh", "card_push", "page_stock")))
check("…all on the slow lane (never in front of a launch or a bid change)",
      all(f'"{k}"' in jb.split("SLOW_KINDS =")[1].split("\n")[1] + jb.split("SLOW_KINDS =")[1].split("\n")[0] for k in ("spark_check", "post_thumbs", "profile_refresh", "card_push", "page_stock")))
check("the sweep retries due checks, copies covers, and schedules stocking on slow cycles",
      "spark_check.run(db, models, tiktok_api, ids=None, limit=10)" in bg and "thumbs.process_pending(db, models, limit=10)" in bg
      and "if slow:\n                    beat(\"page_stock\"); page_stock.schedule(db, models)" in bg)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
