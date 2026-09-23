"""v150 — Instant Page / Instant Form offers and the build queue.

  * Page templates copy a master page through the web API (button re-pointed), browser only
    as a fallback; form templates save the wording once.
  * Forms are read back FIELD BY FIELD — anything that didn't take stops the publish.
  * The queue shows the real step + how long ("stuck?" after 90 s); retry / cancel.
  * "Select all needing" — accounts grouped by Business Center, 'has it' by name.
  * TikTok's hosted preview on every built page / form; launches build from templates.
Functional on fakes (the web API and the browser builder are stubbed); routes source-asserted."""
import importlib, json, os, sys, types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m

ab = importlib.import_module("app.asset_builds")
lfb = importlib.import_module("app.lead_form_builder")

# =======================================================================================
print("-- forms: read back every field --")
edits = {"destination_url": "https://offer.example/fc", "cta_title": "CONTINUE", "question_label": "Which phone?",
         "question_options": ["iPhone", "Android"], "privacy_url": "", "thanks_title": "You Qualify"}
good = {"destination_url": "https://offer.example/fc", "cta_title": "CONTINUE", "question_label": "Which phone?",
        "question_options": ["iPhone", "Android"], "thanks_title": "You Qualify", "privacy_url": "https://old/p"}
check("identical → no differences (an empty field keeps the master's)", lfb.form_differences(edits, good) == [])
bad = dict(good, destination_url="https://master.example/old", question_options=["iPhone", "Android", "Other"])
d = lfb.form_differences(edits, bad)
check("a kept master link and a different answer list are both named", len(d) == 2 and "offer link" in d[0] and "answers" in d[1], d)

# a fake page-editor web API: records what was called, returns the form as "stored"
calls = []
def brick_form(dest="https://master.example/old", q="Old?", opts=("A", "B")):
    return json.dumps({"data": {
        "1": {"name": "LpFormCTA", "brickIndex": "lead-1", "buttonsInfo": {"website": {"title": "GO", "link": {"url": dest}}}},
        "2": {"name": "LpMultipleChoiceField", "label": q, "options": [{"label": o, "key": str(i)} for i, o in enumerate(opts)]},
        "3": {"name": "LpThanksPage", "brickIndex": "lead-2", "title": "Thanks", "description": "d"},
    }})
class FakeWeb:
    POLL_TRIES, POLL_S = 2, 0
    def __init__(self, store_edits=True, publish_drift=False):
        self.store = {"M1": brick_form()}
        self.store_edits, self.publish_drift = store_edits, publish_drift
    def read_page(self, pid, target, owner, shape=""):
        calls.append(("read", pid))
        data = self.store.get(pid)
        pub = brick_form() if (self.publish_drift and pid == "F9") else None
        return ({"code": 0, "data": {"page_info": {"data": data, "publish_data": pub, "business_type": 1}}} if data else {"code": 1}), "s", []
    def _ok(self, r): return r.get("code") == 0
    def explain(self, r, t): return "refused"
    def thumb_uri(self, p): return ""
    def _post(self, path, body, target):
        calls.append(("post", path))
        if path == "/v1/create/":
            self.store["F9"] = self.store["M1"]; return {"code": 0, "data": {"page_id": "F9"}}
        if path == "/v1/update/":
            if self.store_edits:
                self.store["F9"] = body["data"]
            return {"code": 0}
        return {"code": 0}
fw = FakeWeb()
sys.modules["app.instant_page_web"] = fw
steps = []
r = lfb.build_form("M1", "FC form", "A1", edits, source_owner="A0", on_step=steps.append)
check("a form built from the master with the wording written in, read back, published",
      r["ok"] and ("post", "/v1/publish/F9/") in calls and steps[-1] == "5/5 Publishing" and "4/5 Reading it back field by field" in steps, r)
calls.clear()
sys.modules["app.instant_page_web"] = FakeWeb(store_edits=False)
r = lfb.build_form("M1", "FC form", "A1", edits, source_owner="A0")
check("TikTok silently kept the master's wording → NOT published, the difference named",
      not r["ok"] and ("post", "/v1/publish/F9/") not in calls and "offer link" in r["error"] and "Not published" in r["error"], r)
calls.clear()
sys.modules["app.instant_page_web"] = FakeWeb(publish_drift=True)
r = lfb.build_form("M1", "FC form", "A1", edits, source_owner="A0")
check("the published version differs → reported, never counted as built", not r["ok"] and "live form differs" in r["error"])
del sys.modules["app.instant_page_web"]

# =======================================================================================
print("-- pages: web copy first, browser fallback --")
dup_ok = {"v": True}
dups = []
def duplicate(src, name, target, new_url="", new_text="", source_owner=""):
    dups.append((src, name, target, new_url, new_text, source_owner))
    return {"ok": True, "page_id": "P77"} if dup_ok["v"] else {"ok": False, "page_id": "", "error": "TikTok said no"}
mod("app.instant_page_web", duplicate=duplicate)
browser = []
mod("app.instant_page_builder", available=lambda: True,
    build_and_verify=lambda db, acct, tpl, on_step=None: (browser.append(tpl.name), on_step and on_step("Complete"), {"ok": True, "page_id": "B55"})[-1])
mod("app.spark_web_api", load_cookies=lambda: {"sessionid": "x"}, WebAuthError=type("WebAuthError", (Exception,), {}))
class TErr(Exception): pass
mod("app.tiktok_api", TikTokError=TErr)
synced = []
routes_pkg = mod("app.routes"); routes_pkg.__path__ = [os.path.join(ROOT, "app", "routes")]
mod("app.routes.instant_pages", sync_account=lambda db, acct: synced.append(acct.advertiser_id))
class Col:
    def __init__(s, n): s.n = n
    def __eq__(s, v): return lambda r: getattr(r, s.n) == v
    def __ne__(s, v): return lambda r: getattr(r, s.n) != v
    def __hash__(s): return 1
    def in_(s, vals): return lambda r: getattr(r, s.n) in vals
class Row(types.SimpleNamespace): pass
def model(cls_name, **cols):
    cls = type(cls_name, (Row,), {k: Col(k) for k in cols})
    return cls
InstantPage = model("InstantPage", name=1, status=1, owner_advertiser_id=1, page_id=1)
AdAccount = model("AdAccount", advertiser_id=1, owner_user_id=1)
class Q:
    def __init__(s, rows): s.rows = rows
    def filter(s, *c): return Q([r for r in s.rows if all(f(r) for f in c)])
    def filter_by(s, **kw): return Q([r for r in s.rows if all(getattr(r, k) == v for k, v in kw.items())])
    def first(s): return s.rows[0] if s.rows else None
    def __iter__(s): return iter(s.rows)
tpl = types.SimpleNamespace(id=5, name="Free Cash", url="https://lander/fc", button_text="CONTINUE", master_page_id="", master_advertiser_id="", owner_user_id=None)
pages = [InstantPage(name="Free Cash", status="PUBLISHED", owner_advertiser_id="A0", page_id="M100", preview_url="")]
accounts = [AdAccount(advertiser_id="A0", owner_bc_id="BC1", owner_user_id=None), AdAccount(advertiser_id="A1", owner_bc_id="BC1", owner_user_id=None)]
class DB:
    def get(s, M, i): return tpl
    def query(s, M):
        return Q(pages if M is InstantPage else accounts)
    def commit(s): pass
    def rollback(s): pass
M = types.SimpleNamespace(PageTemplate=object, InstantPage=InstantPage, AdAccount=AdAccount)
acct = types.SimpleNamespace(advertiser_id="A1", owner_bc_id="BC1", access_token="t", advertiser_name="Acct 1")
row = types.SimpleNamespace(template_id=5)
def after_sync(db, a):     # the account now lists the copy
    synced.append(a.advertiser_id); pages.append(InstantPage(name="Free Cash", status="PUBLISHED", owner_advertiser_id="A1", page_id="P77", preview_url="https://tt/preview/P77"))
sys.modules["app.routes.instant_pages"].sync_account = after_sync
st = []
r = ab.build_page(DB(), M, row, acct, st.append)
check("no master set → a published page of the same name is copied, button re-pointed to the template",
      r["ok"] and r["method"] == "web copy" and dups[-1] == ("M100", "Free Cash", "A1", "https://lander/fc", "CONTINUE", "A0") and not browser, r)
check("…verified by TikTok listing it on the account, with its preview", r["id"] == "P77" and r["preview"] == "https://tt/preview/P77" and synced == ["A1"])
check("the steps are the real ones", st[0].startswith("1/2 Copying the master page") and st[1].startswith("2/2 Checking"))
pages[:] = [p for p in pages if p.owner_advertiser_id != "A1"]
dup_ok["v"] = False
st = []
r = ab.build_page(DB(), M, row, acct, st.append)
check("the copy refused → the browser builder takes over (and says why)", r["ok"] and r["method"] == "browser" and browser == ["Free Cash"]
      and any("building in the browser" in s for s in st) and "browser · Complete" in st, r)
tpl.master_page_id, tpl.master_advertiser_id = "MX", "A9"
dup_ok["v"] = True
ab.build_page(DB(), M, row, acct, lambda s: None)
check("a template's own master page wins", dups[-1][0] == "MX" and dups[-1][5] == "A9")

# =======================================================================================
print("-- queue view, picker, preview --")
now = datetime(2026, 9, 23, 12, 0, 0)
R = lambda **kw: types.SimpleNamespace(**{"batch": "b1", "kind": "page", "name": "Free Cash", "template_id": 5, "advertiser_id": "A1",
                                           "status": "pending", "step": "", "step_at": None, "method": "", "result_id": "", "preview_url": "",
                                           "error": "", "attempts": 0, "created_at": now - timedelta(minutes=5), "started_at": None, "finished_at": None, **kw})
rows = [R(id=1, status="running", step="1/2 Copying", step_at=now - timedelta(seconds=120), advertiser_id="A1"),
        R(id=2, status="running", step="2/2 Checking", step_at=now - timedelta(seconds=10), advertiser_id="A2"),
        R(id=3, status="failed", error="TikTok said no", advertiser_id="A3"), R(id=4, status="success", result_id="P1", advertiser_id="A4")]
v = ab.view(rows, {"A1": "Alpha"}, now)[0]
check("one line per batch with counts and the last failure", v["counts"] == {"pending": 0, "running": 2, "success": 1, "failed": 1, "cancelled": 0}
      and v["last_error"] == "TikTok said no" and v["active"])
r1 = [x for x in v["rows"] if x["id"] == 1][0]; r2 = [x for x in v["rows"] if x["id"] == 2][0]
check("the real step and how long it's been on it; 'stuck?' after 90 s", r1["secs"] == 120 and r1["stuck"] and not r2["stuck"] and r1["account"] == "Alpha")
check("running first, then waiting, failed, built", {x["id"] for x in v["rows"][:2]} == {1, 2} and v["rows"][-1]["id"] == 4)
A = lambda i, bc, name="": types.SimpleNamespace(advertiser_id=i, owner_bc_id=bc, advertiser_name=name or i)
g = ab.group_targets([A("1", "B1"), A("2", "B1"), A("3", "B2"), A("4", "")], {"B1": "Blue Bat", "B2": "BC 60"}, have={"1"}, busy={"3"})
check("accounts grouped by Business Center, 'has it' and 'queued' marked, needing counted",
      [x["bc_name"] for x in g] == ["Blue Bat", "BC 60", "No Business Center"] and g[0]["needing"] == 1 and g[1]["needing"] == 0
      and g[0]["accounts"][-1] == {"id": "1", "name": "1", "has": True, "busy": False})
check("TikTok's own preview link, else the hosted immers.page one",
      ab.preview_link("P9", "") == "https://sg.immers.page/instant_page/page/P9?mode=preview&type=wrapped" and ab.preview_link("P9", "https://x") == "https://x")
ft = types.SimpleNamespace(destination_url=" https://o ", cta_title="GO", privacy_url="", company_name="Acme", thanks_title="", thanks_description="",
                           question_label="Q?", question_options="Yes\r\nNo\n\n")
check("a form template → the builder's edits", ab.template_edits(ft) == {"destination_url": "https://o", "cta_title": "GO", "privacy_url": "", "company_name": "Acme",
      "thanks_title": "", "thanks_description": "", "question_label": "Q?", "question_options": ["Yes", "No"]})

# =======================================================================================
print("-- wiring --")
abs_ = read("app/asset_builds.py"); rt = read("app/routes/asset_builds_page.py"); ipr = read("app/routes/instant_pages.py")
check("queue skips accounts that have it or are already queued; one job works through it",
      "todo = [i for i in ids if i not in have and i not in busy]" in abs_ and 'jobs.enqueue_once(db, "asset_builds"' in abs_ and '@jobs.handler("asset_builds")' in read("app/job_handlers.py"))
check("dead cookies stop the queue; a restart marks running builds 'interrupted'", 'if res.get("stop"):' in abs_ and "def recover(db, models)" in abs_
      and "_ab.recover(_db, _vm)" in read("app/main.py"))
check("every old template build route now queues", ipr.count("return _queue_template(request, db, tpl_id") == 3 and "instant_page_build" not in ipr.split("def _queue_template")[1].split("def build_on_accounts")[0])
check("routes: targets, queue, live list, retry / cancel / retry-failed, form templates, master read",
      all(x in rt for x in ('"/builds/targets.json"', '"/builds/queue"', '"/builds.json"', '"/builds/{build_id}/retry"', '"/builds/{build_id}/cancel"',
                            '"/builds/batch/{batch}/retry-failed"', '"/lead-forms/templates/save"', '"/lead-forms/master.json"')))
check("only the workspace's templates, accounts and master forms", rt.count("sc.owns(t)") == 1 and "sc.allows(mrow.owner_advertiser_id)" in rt and "sc.allows(r.advertiser_id)" in rt)
cp = read("app/routes/campaigns.py")
check("launches build a missing page / form from its template", "_ab.build_page(db, models" in cp and "_ab.build_form(db, models" in cp and "models.FormTemplate.name == name" in cp)
js = read("app/static/asset-builds.js")
check("UI: 'Select all needing', per-BC select, live queue that polls only while something runs",
      "Select all needing a" in js and "abp-bcall" in js and "if (d.active) M.timer = setTimeout" in js and "stuck?" in js and "Preview ↗" in js)
check("both screens use it", 'AB.mount(document.getElementById("abPages"), "page")' in read("app/templates/instant_pages.html")
      and 'AB.mount(document.getElementById("abForms"), "form")' in read("app/templates/lead_forms.html")
      and 'AB.pickNeeding({ kind: "form"' in read("app/templates/lead_forms.html"))
md = read("app/models.py")
check("models: FormTemplate, AssetBuild, PageTemplate master", "class FormTemplate(Base):" in md and "class AssetBuild(Base):" in md and "master_page_id = Column(String" in md)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
