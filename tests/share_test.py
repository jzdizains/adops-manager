"""v155.9 — Share: a failed launch / blocked account / failed job as paste-ready text."""
import importlib, json, os, sys, types
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

sh = importlib.import_module("app.share")
TECH = "code=40002 message=Invalid budget type. Please check and try again. request_id=20260924055815E950548346C3A477AF3A at=/smart_plus/adgroup/create/"
check("TikTok's request id is read out of the technical line", sh.request_id_of(TECH) == "20260924055815E950548346C3A477AF3A")
req = {"where": "/smart_plus/adgroup/create/", "code": "40002", "message": "Invalid budget type.",
       "body": {"advertiser_id": "7659134748954083346", "budget_mode": "BUDGET_MODE_DAY", "promotion_type": "LEAD_GENERATION"}}
txt = sh.launch_report(preset="Lead preset", account="blue bat_260706030014", advertiser_id="7659134748954083346", campaign_id="",
                       batch_ref="892537", friendly="TikTok rejected a field value.", technical=TECH,
                       steps=[{"t": "21:57:58", "s": "started"}, {"t": "21:58:12", "s": "creating Smart+ campaign"}], request=req,
                       when="2026-09-24 01:58 UTC")
check("the report names preset, account and id, batch", "preset “Lead preset” on blue bat_260706030014 (7659134748954083346)" in txt and "batch 892537" in txt)
check("…the dashboard's message and TikTok's answer verbatim", "TikTok rejected a field value." in txt and TECH in txt)
check("…the steps", "2. 21:58:12 creating Smart+ campaign" in txt)
check("…and the exact request TikTok refused", '"budget_mode": "BUDGET_MODE_DAY"' in txt and "/smart_plus/adgroup/create/" in txt)
check("no request found → says so instead of guessing", "isn't in Diagnostics" in sh.launch_report(preset="", account="", advertiser_id="1", campaign_id="", batch_ref="b",
                                                                                                    friendly="", technical="", steps=None, request=None))
big = dict(req, body={"x": "y" * 9000})
check("a huge request is cut, the report stays pasteable", "… (cut)" in sh.launch_report(preset="", account="", advertiser_id="1", campaign_id="", batch_ref="b",
                                                                                           friendly="", technical="", steps=None, request=big))

# find_request against stand-in rows (the query chain only needs filter/order_by/first/limit)
class Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a, **k): return self
    def order_by(self, *a): return self
    def limit(self, n): return self.rows[:n]
    def first(self): return self.rows[0] if self.rows else None
    def __iter__(self): return iter(self.rows)
class Col:
    def __eq__(self, o): return True
    def __ge__(self, o): return True
    def __le__(self, o): return True
    def desc(self): return self
models = types.SimpleNamespace(DiagEvent=types.SimpleNamespace(request_id=Col(), id=Col(), kind=Col(), last_at=Col(), first_at=Col()))
row = types.SimpleNamespace(where="/smart_plus/adgroup/create/", code="40002", message="Invalid budget type. Please check and try again.",
                            request_id="20260924055815E950548346C3A477AF3A", context=json.dumps({"method": "POST", "body": req["body"]}))
db = types.SimpleNamespace(query=lambda m: Q([row]))
log = types.SimpleNamespace(error_technical=TECH, advertiser_id="7659134748954083346", created_at=datetime(2026, 9, 24, 1, 58))
f = sh.find_request(db, models, log)
check("the refused request is found in Diagnostics (by request id)", f and f["body"]["budget_mode"] == "BUDGET_MODE_DAY" and f["where"].startswith("/smart_plus"))
check("nothing in Diagnostics → None", sh.find_request(types.SimpleNamespace(query=lambda m: Q([])), models, log) is None)

print("-- wiring --")
cp = read("app/routes/campaigns.py")
check("share route: read-only, scoped to the viewer's accounts, only its own batch",
      '@router.get("/campaigns/result/{batch_ref}/share/{log_id}")' in cp and "log.batch_ref != batch_ref or not sc.allows(log.advertiser_id)" in cp)
check("…declared before the result page's own route", cp.index('"/campaigns/result/{batch_ref}/share/{log_id}"') < cp.index('@router.get("/campaigns/result/{batch_ref}")'))
lr = read("app/templates/launch_result.html")
check("a Share button on every FAILED account of a launch", 'data-share-url="/campaigns/result/{{ l.batch_ref }}/share/{{ l.id }}"' in lr and "{% if not l.ok %}" in lr.split("share-btn")[0][-400:])
check("…and on failed jobs", 'data-share-title="FAILED JOB #' in read("app/templates/jobs.html"))
rv = read("app/static/launch-review.js")
check("Review: Share per blocked account and Share all", "lr-share" in rv and "lr-share-all" in rv and "shareRows(" in rv)
ui = read("app/static/ui.js")
check("one helper: clipboard, then the old copy command, then a pop-up with the text selected",
      "UI.share = function" in ui and "navigator.clipboard" in ui and 'execCommand("copy")' in ui and "Copy this and paste it" in ui)
check("never sends anything anywhere — it only copies", "fetch(" not in ui.split("UI.share = function")[1].split("document.addEventListener")[0])
print("-- New form on several accounts (v155.10) --")
lf = read("app/routes/lead_forms.py")
check("one account builds right away (as before); several → one background job, accounts that already have it skipped",
      "if len(ids) > 1:" in lf and '"lead_form_build_many"' in lf and "todo = [i for i in ids if i not in have]" in lf and "target_advertiser_id = ids[0]" in lf)
check("every picked account must be in the viewer's workspace", "not all(sc.allows(i) for i in ids)" in lf)
check("the job: same verified build per account, no-access grouped into one line, dead cookies stop it",
      "def build_on_many(" in lf and "lead_form_builder.build_form(template_form_id, name, adv, edits" in lf.split("def build_on_many(")[1][:2500]
      and "no_access_summary" in lf.split("def build_on_many(")[1][:4000] and "except spark_web_api.WebAuthError" in lf.split("def build_on_many(")[1][:2500])
jh = read("app/job_handlers.py")
check("job handler registered, on the slow lane", '@jobs.handler("lead_form_build_many")' in jh and '"lead_form_build_many"' in read("app/jobs.py").split("SLOW_KINDS")[1][:400])
print("-- presets can pick a Form template (v155.11) --")
import types as _t
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader, ChainableUndefined
_env = Environment(loader=ChoiceLoader([DictLoader({"base.html": "{% block content %}{% endblock %}{% block scripts %}{% endblock %}"}),
                                        FileSystemLoader(os.path.join(ROOT, "app/templates"))]), undefined=ChainableUndefined, autoescape=True)
_env.filters.update(local=lambda *a, **k: "", ago=lambda *a, **k: "", money=str, strip_key=str, js=lambda v: json.dumps(v, default=lambda o: None))
_env.policies["json.dumps_kwargs"] = {"default": lambda o: None}
_h = _env.get_template("template_form.html").render(
    blob={"lead_form_name": "Games 18+"}, lead_forms=[{"name": "Games 18+", "count": 3}, {"name": "Freecash", "count": 40}],
    form_templates=[_t.SimpleNamespace(id=1, name="Games 18+"), _t.SimpleNamespace(id=2, name="Sweeps US")], request=None)
_sel = _h.split('name="lead_form_name"')[1].split("</select>")[0]
check("templates listed first, marked, with how many accounts already have it", "Form templates — built at launch" in _sel
      and "Games 18+ — template · already on 3 account(s)" in _sel and "Sweeps US — template<" in _sel.replace("\n", ""))
check("an existing form that is also a template isn't listed twice; other forms stay", _sel.count(">Games 18+ —") == 1 and "Freecash — on 40 account(s)" in _sel)
check("the stored choice stays selected", 'value="Games 18+" selected' in _sel)
check("the preset page gets this workspace's templates (with a master form)",
      '"form_templates": sc.owned(db.query(models.FormTemplate), models.FormTemplate)' in read("app/routes/templates_routes.py"))
check("the launch builds a missing form from the template of that name (already in place since v150)",
      "models.FormTemplate.name == name" in read("app/routes/campaigns.py") and "_ab.build_form(" in read("app/routes/campaigns.py"))
print("-- Who connected TikTok (v155.12) --")
import ast as _ast
_dsrc = read("app/routes/diagnostics.py")
_ns = {}
for _n in _ast.parse(_dsrc).body:
    if isinstance(_n, _ast.FunctionDef) and _n.name == "group_connections":
        exec(compile(_ast.Module([_n], []), "diag", "exec"), _ns)
_A = lambda i, n, t: types.SimpleNamespace(advertiser_id=i, advertiser_name=n, access_token=t)
_g = _ns["group_connections"]([_A("1", "blue bat 014", "tokA"), _A("2", "blue bat 017", "tokA"), _A("3", "A1 Prime", "tokB"), _A("4", "no token", "")])
check("accounts grouped by the connection they use, biggest first, accounts without a token left out",
      [len(g["accounts"]) for g in _g] == [2, 1] and _g[0]["accounts"] == ["blue bat 014", "blue bat 017"])
check("…only a short fingerprint identifies a connection (never the token)", len(_g[0]["key"]) == 16 and "tokA" not in _g[0]["key"])
_route = _dsrc.split('def connections_json(')[1].split("\n@router")[0]
check("the JSON the page gets never carries the token", '"token"' not in _route.split("out.append(")[1] and "g[\"token\"]" not in _route.split("out.append(")[1])
check("owner only, DB connection released before calling TikTok, answers cached", "if not guard.is_owner(request):" in _route
      and _route.index("db.rollback()") < _route.index("tiktok_api.user_info(") and "_WHO[g[\"key\"]] = (" in _route)
check("the TikTok call is the read-only /user/info/", 'return api_get("/user/info/", access_token) or {}' in read("app/tiktok_api.py"))
_dt = read("app/templates/diagnostics.html")
check("Diagnostics card, loaded only when opened (the page stays fast)", 'id="connections"' in _dt and 'card.addEventListener("toggle"' in _dt and "/diagnostics/connections.json" in _dt)
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
