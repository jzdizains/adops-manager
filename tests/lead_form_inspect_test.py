"""Lead-form Inspect — read-only definition dump (v137).

A form's content (headline, questions, privacy URL, thank-you) lives in an opaque `data`
JSON. Before any builder can EDIT those fields it has to be mapped, so this read-only
endpoint reads one form through the page-editor web session and returns its parsed
definition. It must never write or create anything.

Pure route behaviour (fastapi/sqlalchemy stubbed), plus template + read-only asserts."""
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

class JSONResponse:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code

class WebAuthError(Exception):
    pass

class _Router:
    def _deco(self, *a, **k):
        def wrap(fn): return fn      # leave the route function callable for the test
        return wrap
    get = _deco
    post = _deco

_mod("fastapi", APIRouter=_Router, Depends=_Any(), Form=_Any(), Request=_Any)
_mod("fastapi.responses", JSONResponse=JSONResponse, RedirectResponse=_Any)
_mod("sqlalchemy.orm", Session=_Any)
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.models"); _mod("app.queries", enabled_accounts=lambda db: [])
_mod("app.tiktok_api"); _mod("app.database", get_db=lambda: None); _mod("app.templating", render=lambda *a, **k: None)
_mod("app.spark_web_api", load_cookies=lambda: True, WebAuthError=WebAuthError)
# instant_page_web is imported INSIDE the route
FORM_JSON = '{"data":{"c1":{"type":"title","content":{"text":"Sign up now"}},' \
            '"c2":{"type":"privacy","link":{"url":"https://ex.com/privacy"}}}}'
def _read_page(fid, adv, source_owner=""):
    return ({"code": "0", "data": {"page_info": {
        "data": FORM_JSON, "business_type": 9, "title": "My Form", "template_id": "tmpl_1"}}}, "target", ["target: 0"])
_mod("app.instant_page_web",
     read_page=_read_page,
     _ok=lambda b: str((b or {}).get("code", "")) == "0",
     explain=lambda b, a: "explained")

class _Scope:
    def __init__(self, ok=True): self._ok = ok
    def allows(self, adv): return self._ok
_mod("app.scope", for_request=lambda req, db: _Scope(True))

import importlib
lf = importlib.import_module("app.routes.lead_forms")

class _DB:
    def __init__(self): self.commits = 0
    def commit(self): self.commits += 1
    def query(self, *a, **k): return self
    def filter_by(self, *a, **k): return self
    def first(self): return None

print("-- read-only inspect returns the parsed definition --")
db = _DB()
res = lf.inspect(request=object(), form_id="7688359276615762197", advertiser_id="7659322473418194952", db=db)
c = res.content
check("returns ok", c.get("ok") is True, c)
check("never writes to the DB (read-only)", db.commits == 0, db.commits)
check("parses the form's data JSON into an object", isinstance(c.get("definition"), dict) and "data" in c["definition"])
check("the headline component is reachable in the parsed tree",
      c["definition"]["data"]["c1"]["content"]["text"] == "Sign up now")
check("the privacy link component is reachable", c["definition"]["data"]["c2"]["link"]["url"] == "https://ex.com/privacy")
check("carries title + business_type + read shape for mapping", c.get("title") == "My Form" and c.get("business_type") == 9 and c.get("read_shape") == "target")
check("also returns the human-facing fields the phone preview renders", isinstance(c.get("fields"), dict) and "question_label" in c["fields"] and "destination_url" in c["fields"])

print("-- extract_form_fields reads a real brick tree the way the preview shows it --")
import app.lead_form_builder as lfb
BRICKS = {"data": {
    "b1": {"name": "LpAgreement", "companyName": "Acme", "linkList": [{"linkUrl": "https://ex.com/p", "linkText": "x"}]},
    "b2": {"name": "LpMultipleChoiceField", "label": "Are you 18+?", "options": [{"label": "Yes"}, {"label": "No"}]},
    "b3": {"name": "LpThanksPage", "brickIndex": "lead-1", "title": "You're in!", "description": "Tap below"},
    "b4": {"name": "LpFormCTA", "brickIndex": "lead-2", "buttonsInfo": {"website": {"title": "Start", "link": {"url": "https://go.example/offer"}}}},
    "b5": {"name": "LpThanksPage", "brickIndex": "nonLead-9", "title": "Sorry", "description": "not eligible"},
}}
import json as _json
ff = lfb.extract_form_fields(_json.dumps(BRICKS), title="Games 18+")
check("pulls the lead question, options, company, CTA and destination — nonLead is ignored",
      ff["question_label"] == "Are you 18+?" and ff["question_options"] == ["Yes", "No"]
      and ff["company_name"] == "Acme" and ff["cta_title"] == "Start"
      and ff["destination_url"] == "https://go.example/offer" and ff["thanks_title"] == "You're in!"
      and ff["title"] == "Games 18+")
check("bad/empty definition never crashes the extractor", lfb.extract_form_fields("not json")["question_label"] == "" and lfb.extract_form_fields(None)["question_options"] == [])

print("-- guards --")
lf.scope_mod = None  # not used; scope stubbed via sys.modules
import app.scope as _scopemod
_scopemod.for_request = lambda req, db: _Scope(False)
res2 = lf.inspect(request=object(), form_id="x", advertiser_id="y", db=_DB())
check("an account outside the workspace is refused (403, no read)", res2.content.get("ok") is False and res2.status_code == 403)
_scopemod.for_request = lambda req, db: _Scope(True)
import app.spark_web_api as _sw
_sw.load_cookies = lambda: False
res3 = lf.inspect(request=object(), form_id="x", advertiser_id="y", db=_DB())
check("no cookies → clear error, still no crash", res3.content.get("ok") is False and "cookies" in res3.content.get("error", "").lower())

print("-- source is read-only + wired --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
src = read("app/routes/lead_forms.py")
check("inspect is a GET route", '@router.get("/lead-forms/inspect")' in src)
_body = src.split("def inspect(")[1].split("\ndef ")[0]     # inspect() only, up to the next def
check("inspect writes nothing (no commit/add/delete in the route body)",
      "db.commit" not in _body and ".add(" not in _body and ".delete(" not in _body)

print("-- template --")
t = read("app/templates/lead_forms.html")
check("a Preview button carries the form id + owner account (group + per-account)", 'lf-preview' in t and 'data-form="{{ g.rep_form_id }}"' in t and 'data-form="{{ a.form_id }}"' in t)
check("clicking it reads the form live and renders it in the TikTok-style phone (no raw-JSON dump)",
      '/lead-forms/inspect?form_id=' in t and 'j.fields' in t and 'lfp-phone' in t and 'lf-inspect' not in t and 'lf-json' not in t)
check("STATIC_VERSION bumped", 'STATIC_VERSION = "158"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
