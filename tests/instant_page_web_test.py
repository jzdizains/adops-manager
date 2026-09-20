"""Instant Page duplication through the page editor's web API (v120), against a fake
TikTok (httpx MockTransport) that behaves exactly as the recorded flow describes:
- page_info(source, account_id=TARGET) → create(duplicate_id) → publish; account_id is
  always the target; Referer carries the target's library page; csrf header present
- a changed link: poll page_info until the copy is readable ("record not found" twice),
  update every component with a string link.url (map.Button ignored), read back and
  refuse to publish when the old link is still there; nothing with a link → refused
- thumbnail_uri: the tos-…/hex form, cut out of `thumbnail` when missing
- code 200000 / HTML answer = dead cookies → WebAuthError (the run stops)
- clone_to_many: skips accounts that already list the name, stops the run on dead
  cookies, never stops it on a refused account
Static: routes carry new_url/new_text, UI un-retired, launch copies from a sibling
before building. Runs with httpx installed."""
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
ipw = importlib.import_module("app.instant_page_web")
ipw.POLL_S = 0.0

COOKIES = {"sessionid_ads": "s", "sid_guard_ads": "g", "csrftoken": "c"}
cf.write_text(json.dumps({"cookies": COOKIES, "saved_at": "now"}))

seen = []
def fake(handler):
    def _client(cookies=None):
        cookies = cookies if cookies is not None else w.load_cookies()
        headers = {"X-CSRFToken": cookies.get("csrftoken", ""), "Referer": w.ADS_BASE + "/"}
        return httpx.Client(base_url=w.ADS_BASE, cookies=cookies, headers=headers, transport=httpx.MockTransport(handler), follow_redirects=False)
    w._client = _client

SRC_DATA = json.dumps({"map": {"Button": "ghost-id"}, "data": {
    "c1": {"type": "text", "content": {"text": "hello"}},
    "c2": {"type": "button", "link": {"url": "https://old.example/offer"}, "content": {"text": "Continue"}},
    "c3": {"type": "button", "link": {"url": "https://old.example/offer2"}},
}})
SOURCE = {"data": SRC_DATA, "template_id": 77, "thumbnail": "https://p16-sign.tiktokcdn.com/tos-alisg-i-abc/0123abcd~tplv.png", "business_type": 6}

class Fake:
    """The recorded endpoints. state: pages created, their data, publish calls, 'record not found' countdown."""
    def __init__(self, dead=False, html=False):
        self.pages = {"900": dict(SOURCE)}; self.created = []; self.updated = []; self.published = []; self.nf = {}; self.dead = dead; self.html = html; self.next_id = 1000
    def __call__(self, req):
        seen.append(req)
        if self.html:
            return httpx.Response(200, text="<html>login</html>")
        if self.dead:
            return httpx.Response(200, json={"code": 200000, "msg": "please log into your user account", "data": None})
        body = json.loads(req.content or b"{}")
        path = req.url.path
        if path.startswith("/instant_page/api/v1/page_info/"):
            pid = path.rstrip("/").split("/")[-1]
            if self.nf.get(pid, 0) > 0:
                self.nf[pid] -= 1
                return httpx.Response(200, json={"code": 40001, "msg": "record not found", "data": None})
            p = self.pages.get(pid)
            if not p:
                return httpx.Response(200, json={"code": 40001, "msg": "record not found", "data": None})
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {"page_info": {**p, "publish_data": p.get("publish_data", "")}}})
        if path == "/instant_page/api/v1/create/":
            self.created.append(body)
            if body.get("thumbnail_uri", "").startswith("http"):
                return httpx.Response(200, json={"code": 40002, "msg": "invalid thumbnail url", "data": None})
            pid = str(self.next_id); self.next_id += 1
            self.pages[pid] = {**self.pages[body["duplicate_id"]], "title": body["title"]}    # edits to `data` ignored, as recorded
            self.nf[pid] = 2
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {"page_id": pid}})
        if path == "/instant_page/api/v1/update/":
            self.updated.append(body)
            self.pages[str(body["page_id"])]["data"] = body["data"]
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {}})
        if path.startswith("/instant_page/api/v1/publish/"):
            pid = path.rstrip("/").split("/")[-1]
            self.published.append(pid)
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {}})
        return httpx.Response(404, text="not found")

print("\n-- plain duplicate --")
f = Fake(); fake(f); seen.clear()
r = ipw.duplicate("900", "FC UK", "555")
check("read → create(duplicate_id) → publish, in that order, all POST", r["ok"] and r["page_id"] == "1000" and [q.url.path for q in seen] == ["/instant_page/api/v1/page_info/900/", "/instant_page/api/v1/create/", "/instant_page/api/v1/publish/1000/"] and all(q.method == "POST" for q in seen), str(r))
check("account_id is the TARGET on every call; Referer is the target's library page; csrf header set",
      all(json.loads(q.content)["account_id"] == "555" for q in seen) and all(q.headers.get("referer") == "https://ads.tiktok.com/i18n/material/instantPage?aadvid=555" for q in seen) and all(q.headers.get("x-csrftoken") == "c" for q in seen))
c = f.created[0]
check("create carries business_type, data, duplicate_id, template_id, title, thumbnail_uri (tos form cut from the thumbnail URL)",
      c["business_type"] == 6 and c["data"] == SRC_DATA and c["duplicate_id"] == "900" and c["template_id"] == 77 and c["title"] == "FC UK" and c["thumbnail_uri"] == "tos-alisg-i-abc/0123abcd", str(c))
check("no update when the link isn't changed", not f.updated and f.published == ["1000"])

print("\n-- re-pointed link --")
f = Fake(); fake(f); seen.clear()
r = ipw.duplicate("900", "FC UK", "555", new_url="https://new.example/lp", new_text="Go")
paths = [q.url.path for q in seen]
check("polls page_info on the NEW page past 'record not found', then updates, reads back, publishes", r["ok"] and paths.count("/instant_page/api/v1/page_info/1000/") >= 3 and paths.index("/instant_page/api/v1/update/") < paths.index("/instant_page/api/v1/publish/1000/"), str(paths))
doc = json.loads(f.updated[0]["data"])
check("every component with a string link.url is re-pointed (+ label), map.Button ignored, others untouched",
      doc["data"]["c2"]["link"]["url"] == "https://new.example/lp" and doc["data"]["c2"]["content"]["text"] == "Go" and doc["data"]["c3"]["link"]["url"] == "https://new.example/lp" and "link" not in doc["data"]["c1"] and doc["map"]["Button"] == "ghost-id")
check("update carries page_id, title, template_id, thumbnail_uri, account_id", f.updated[0]["page_id"] == "1000" and f.updated[0]["title"] == "FC UK" and f.updated[0]["template_id"] == 77 and f.updated[0]["thumbnail_uri"] == "tos-alisg-i-abc/0123abcd" and f.updated[0]["account_id"] == "555")
check("steps say what happened", r["steps"] == ["read source", "created 1000", "button re-pointed and verified", "published"], str(r["steps"]))

f = Fake(); f.pages["901"] = {**SOURCE, "data": json.dumps({"data": {"c1": {"content": {"text": "no button"}}}})}; fake(f)
r = ipw.duplicate("901", "X", "555", new_url="https://new.example/lp")
check("a page with nothing to re-point is refused BEFORE anything is created", not r["ok"] and "no button with a link" in r["error"] and not f.created)

class Stubborn(Fake):
    def __call__(self, req):
        resp = super().__call__(req)
        if req.url.path == "/instant_page/api/v1/update/":      # says OK, keeps the old data
            self.pages[str(json.loads(req.content)["page_id"])]["data"] = SRC_DATA
        return resp
f = Stubborn(); fake(f)
r = ipw.duplicate("900", "FC UK", "555", new_url="https://new.example/lp")
check("update accepted but the old link reads back → not published, loud error", not r["ok"] and "read back without the new link" in r["error"] and not f.published)

print("\n-- read shape probing --")
class Picky(Fake):
    """page_info answers 'Internal system error' unless account_id is the page's OWNER (as seen live 18 Sep for the recorded shape)."""
    def __call__(self, req):
        if req.url.path.startswith("/instant_page/api/v1/page_info/") and json.loads(req.content).get("account_id") != "111":
            seen.append(req)
            return httpx.Response(200, json={"code": 100000, "msg": "Internal system error. ", "data": None})
        return super().__call__(req)
f = Picky(); fake(f); seen.clear(); notes.clear()
r = ipw.duplicate("900", "FC UK", "555", source_owner="111")
paths = [(q.url.path, json.loads(q.content).get("account_id")) for q in seen]
check("recorded shape refused → the source-owner read is tried (a READ, nothing created in between), then create/publish carry the TARGET as recorded",
      r["ok"] and paths[0] == ("/instant_page/api/v1/page_info/900/", "555") and paths[1] == ("/instant_page/api/v1/page_info/900/", "111")
      and f.created[0]["account_id"] == "555" and f.published == ["1000"] and r["steps"][0] == "read shape: source-owner", str((paths, r)))
check("the refusal is on Diagnostics with TikTok's FULL answer", any("Internal system error" in str(n) and "answer:" in str(n) for n in notes))
f = Picky(); fake(f); seen.clear()
r = ipw.duplicate("900", "FC UK", "555")          # no owner known → target, aadvid, int
check("with no owner known every other shape is probed and the error lists what each answered", not r["ok"] and "tried target: 100000" in r["error"] and "target-int: 100000" in r["error"] and not f.created, r.get("error"))

class NoAccess(Fake):
    """What TikTok answered live on 18 Sep: the login has no access to the account named in account_id."""
    def __call__(self, req):
        if req.url.path.startswith("/instant_page/api/v1/page_info/"):
            seen.append(req)
            return httpx.Response(200, json={"code": 100000, "message": "Internal system error. ", "data": {"err_msg": "RPCError{PSM:[ad.advertiser.adv_info_i18n] Method:[GetLoginAdvInfoByUid] ErrType:[RPC_STATUS_CODE_NOT_ZERO] BizStatusCode:[2901] BizStatusMessage:[not any access permission]}"}})
        return super().__call__(req)
f = NoAccess(); fake(f); seen.clear()
r = ipw.duplicate("900", "FC UK", "555", source_owner="111")
check("'not any access permission' is read out of err_msg: one probe only, nothing created, the message says whose login lacks what",
      not r["ok"] and len(seen) == 1 and not f.created and "no access to ad account 555" in r["error"] and "Business Center" in r["error"] and "Cookies page" in r["error"], r.get("error"))

print("\n-- dead cookies --")
f = Fake(dead=True); fake(f)
try:
    ipw.duplicate("900", "FC UK", "555"); dead = ""
except w.WebAuthError as e:
    dead = str(e)
check("code 200000 raises WebAuthError (the run stops)", "200000" in dead)
f = Fake(html=True); fake(f)
try:
    ipw.duplicate("900", "FC UK", "555"); dead = ""
except w.WebAuthError as e:
    dead = str(e)
check("an HTML answer raises WebAuthError too", "HTML" in dead)
check("thumbnail_uri: kept when already tos form; cut from the URL when a full URL", ipw.thumb_uri({"thumbnail_uri": "tos-x/ab12"}) == "tos-x/ab12" and ipw.thumb_uri({"thumbnail_uri": "https://p16.tiktokcdn.com/tos-y/cd34~tplv.png"}) == "tos-y/cd34" and ipw.thumb_uri({}) == "")

print("\n-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
ip = read("app/routes/instant_pages.py"); ca = read("app/routes/campaigns.py"); th = read("app/templates/instant_pages.html"); lf = read("app/routes/lead_forms.py")
check("clone_one: recorded flow + official /page/get/ verification", "instant_page_web.duplicate(page_id, name, acct.advertiser_id, new_url=new_url, new_text=new_text, source_owner=source_owner)" in ip and "sync_account(db, acct)" in ip.split("def clone_one")[1][:1200] and "doesn't list it on the account" in ip)
body = ip.split("def clone_to_many")[1]
check("clone_to_many: skips accounts that already hold the name, stops the run on dead cookies, pauses, never stops on a refusal", "a re-run never duplicates" in body and "except spark_web_api.WebAuthError" in body and "stopped = True" in body and "_time.sleep(1.5)" in body and 'failed.append(f"{label}: {r[\'error\'][:140]}")' in body)
check("routes carry the optional re-point; the link must be http(s)", 'new_url: str = Form("")' in ip and ip.count("The new button link must start with http:// or https://") == 2 and '"new_url": new_url' in ip)
check("the dead /api/v1/page/copy/ call is gone", "api/v1/page/copy" not in read("app/spark_web_api.py").split("# (the old clone_instant_page")[0] and "clone_instant_page(" not in ip and "clone_instant_page(" not in lf)
js = read("app/static/instant-pages.js")
check("UI (v132): clone button per page name, only with a published copy; optional link + text and test-one-first wording live in the page JS", 'class="btn sm ip-clone" {{ \'disabled\' if not web_ready or not g.source }}' in th and "Clone (test)" in js and "data-url" in js and "new_url: x.new_url" in js)
check("launch: an account without the page copies it from a sibling (same BC first) before building from the template", "def copy_page_from_sibling" in ca and "copied = copy_page_from_sibling(db, acct, name)" in ca and "ip.clone_one(db, src.page_id, name, acct, source_owner=src.owner_advertiser_id)" in ca and 'models.InstantPage.status == "PUBLISHED"' in ca)
check("lead forms ride the same recorded flow, verified by re-read, stop on dead cookies", "instant_page_web.duplicate(form_id, name, acct.advertiser_id, source_owner=from_advertiser_id)" in lf and "stopped = True" in lf.split("def clone_to_many")[1] and "{% if False %}" not in read("app/templates/lead_forms.html"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
