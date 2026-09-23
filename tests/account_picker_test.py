"""Reusable account-picker pop-up (v138).

The launcher's rich account selection (search + Business Center chips + Fresh/Used/Live/
Blocked tabs + grouped multi-select) is now a shared modal — UI.pickAccounts — fed by
/accounts/picker.json, replacing the old single-account dropdowns. First wiring: the Lead
Forms "Clone to…" now multi-selects accounts and clones to all of them in one job.

Pure clone-multi logic (stubbed) + source/component asserts. The launcher's own picker is
untouched (separate component)."""
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

class _Router:
    def _deco(self, *a, **k):
        def wrap(fn): return fn
        return wrap
    get = _deco
    post = _deco

_mod("fastapi", APIRouter=_Router, Depends=_Any(), Form=_Any(), Request=_Any)
_mod("fastapi.responses", JSONResponse=_Any, RedirectResponse=_Any)
_mod("sqlalchemy.orm", Session=_Any)
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.models", LeadForm=object, AdAccount=object, BusinessCenter=object); _mod("app.queries", enabled_accounts=lambda db: []); _mod("app.tiktok_api")
_mod("app.database", get_db=lambda: None); _mod("app.templating", render=lambda *a, **k: None)
_mod("app.spark_web_api", load_cookies=lambda: True, WebAuthError=type("E", (Exception,), {}))

ENQUEUED = {}
_mod("app.jobs", enqueue=lambda db, kind, title, payload, href="": (ENQUEUED.update(kind=kind, payload=payload) or types.SimpleNamespace(id=7)))
class _Scope:
    def allows(self, adv): return True
_mod("app.scope", for_request=lambda req, db: _Scope())

import importlib
lf = importlib.import_module("app.routes.lead_forms")

class _DB:
    def __init__(self, existing=()): self._existing = list(existing)
    def query(self, *a, **k): return self
    def filter_by(self, *a, **k): return self
    def all(self): return [types.SimpleNamespace(owner_advertiser_id=x) for x in self._existing]

print("-- clone-multi enqueues one verified job for the picked accounts --")
ENQUEUED.clear()
lf.clone_multi(request=object(), form_id="F1", from_advertiser_id="900", name="Freecash",
               target_ids="100, 200 ,300,200,900", db=_DB(existing=[]))
check("de-dupes, drops the source account, clones to the rest",
      ENQUEUED.get("payload", {}).get("targets") == ["100", "200", "300"], ENQUEUED.get("payload"))
check("uses the existing verified clone job (one at a time, re-read each)", ENQUEUED.get("kind") == "lead_form_clone_all")

print("-- accounts that already have the form are skipped; nothing is queued if all do --")
ENQUEUED.clear()
lf.clone_multi(request=object(), form_id="F1", from_advertiser_id="900", name="Freecash",
               target_ids="100,200", db=_DB(existing=["100", "200"]))
check("no job is queued when every picked account already has the form", "payload" not in ENQUEUED, ENQUEUED)

print("-- source + component wiring --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
sl = read("app/routes/super_launcher.py")
check("a shared /accounts/picker.json feeds the picker (accounts + BCs + state counts)",
      '@router.get("/accounts/picker.json")' in sl and '"accounts": out' in sl and '"bcs": bclist' in sl and 'account_picker_context(' in sl)
js = read("app/static/account-picker-modal.js")
check("UI.pickAccounts opens a modal fed by that endpoint, with search + BC + status + multi-select",
      "UI.pickAccounts = function" in js and "/accounts/picker.json" in js and "apk-tab" in js and "apk-bc" in js and "apk-cb" in js and "resolve(out)" in js)
b = read("app/templates/base.html")
check("the component is loaded on every page", "account-picker-modal.js" in b)
t = read("app/templates/lead_forms.html")
check("Clone to… now opens the pop-up and posts the multi-select to clone-multi",
      "UI.pickAccounts({ title:" in t and 'action = "/lead-forms/clone-multi"' in t and "target_ids" in t)
check("the old single-account + whole-BC clone dropdowns are gone", 'action="/lead-forms/clone-bc"' not in t and 'name="to_advertiser_id"' not in t)
lfr = read("app/routes/lead_forms.py")
check("clone-multi guards cookies + scope like the other clone routes", '@router.post("/lead-forms/clone-multi")' in lfr and "load_cookies()" in lfr and "sc.allows(from_advertiser_id)" in lfr)

print("-- rolled into Instant Pages too --")
ipr = read("app/routes/instant_pages.py")
ipjs = read("app/static/instant-pages.js")
check("Instant Pages Clone to… uses the same pop-up + a clone-multi job", '@router.post("/instant-pages/clone-multi")' in ipr and "UI.pickAccounts({" in ipjs and 'postForm("/instant-pages/clone-multi"' in ipjs)
check("the pop-up supports optional extra fields (the page's button re-point), back-compat for id-only callers",
      "opts.extra && opts.extra.length" in js and "out = { ids: ids, values: vals }" in js and 'name: "new_url"' in ipjs)
iph = read("app/templates/instant_pages.html")
check("Instant Pages Build on… opens the needs-a-page picker (v150 build queue); build-multi still queues",
      '@router.post("/instant-pages/templates/{tpl_id}/build-multi")' in ipr and 'AB.pickNeeding({ kind: "page"' in iph)
check("the old single-account / whole-BC build dropdowns are gone from the template",
      'class="tpl-one"' not in iph and 'class="tpl-bc"' not in iph)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
