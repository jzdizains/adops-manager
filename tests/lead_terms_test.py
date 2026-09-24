"""v155.13 — TikTok's Lead Generation Terms per ad account: read and, on the operator's button only,
signed exactly as Ads Manager does (recorded 24 Sep 2026 on blue bat_260706030021)."""
import ast, importlib, json, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

sys.modules.setdefault("app.diag", types.SimpleNamespace(record=lambda *a, **k: None))
lt = importlib.import_module("app.lead_terms")
web = lt.spark_web_api

# a stand-in TikTok: which (account, type) pairs are signed; every call recorded
signed = {("014", 16): False, ("014", 70): False, ("021", 16): True, ("021", 70): True}
calls = []
class Resp:
    def __init__(self, body): self.body = body
def fake_send(method, path, *, params=None, payload=None, headers=None):
    calls.append((method, path, dict(params or {}), payload, headers))
    if path == lt.QUERY:
        return Resp({"code": 0, "msg": "success", "data": {"is_exist": signed.get((params["aadvid"], params["setting_type"]), False)}})
    if path == lt.SIGN:
        signed[(params["aadvid"], payload["setting_type"])] = True
        return Resp({"code": 0, "msg": "success", "data": {}})
    return Resp({"code": 404})
web._send = fake_send
web._web_parse = lambda resp, path="": resp.body
web.load_cookies = lambda: {"sessionid": "x", "csrftoken": "y"}

print("-- reading --")
check("an account where a lead ad was built by hand reads as accepted", lt.status("021") is True)
check("one where it never was reads as not accepted", lt.status("014") is False)
calls.clear()
check("…answers are cached (no second TikTok call)", lt.status("021") is True and lt.status("014") is False and calls == [])
check("the check reads BOTH agreements Ads Manager signs (16/1 and 70/2)", lt.AGREEMENTS == ((16, 1), (70, 2)))
web.load_cookies = lambda: {}
check("no cookies → can't tell (None), never a guess", lt.status("999", fresh=True) is None)
web.load_cookies = lambda: {"sessionid": "x", "csrftoken": "y"}

print("-- accepting (only what the button calls) --")
calls.clear()
ok, msg = lt.accept("014")
signs = [c for c in calls if c[1] == lt.SIGN]
check("signs exactly as recorded: POST general_sign ?aadvid=…&req_src=ad_creation with 16/1 then 70/2",
      [(c[0], c[2], c[3]) for c in signs] == [("POST", {"aadvid": "014", "req_src": "ad_creation"}, {"setting_type": 16, "setting_dimension": 1}),
                                             ("POST", {"aadvid": "014", "req_src": "ad_creation"}, {"setting_type": 70, "setting_dimension": 2})], str(signs))
check("…reads it back and reports accepted, cache updated", ok and msg == "accepted" and lt.status("014") is True)
calls.clear()
lt.accept("021")
check("an account that already has them isn't signed again", not [c for c in calls if c[1] == lt.SIGN])
check("the Referer is the account's own Ads Manager page", all("aadvid=" in (c[4] or {}).get("Referer", "") for c in calls))
def refuse(method, path, **k):
    if path == lt.SIGN:
        return Resp({"code": 40002, "msg": "no"})
    return fake_send(method, path, **k)
web._send = refuse
signed[("777", 16)] = False
ok, msg = lt.accept("777")
check("TikTok refusing → not accepted, with its answer", not ok and "40002" in msg)
web._send = fake_send

print("-- review + launch wiring --")
lr = read("app/launch_review.py")
ns = {}
for n in ast.parse(lr).body:
    if (isinstance(n, ast.FunctionDef) and n.name in ("terms_cell", "verdict", "cell")) or \
       (isinstance(n, ast.Assign) and ("TERMS_NOT" in ast.unparse(n.targets[0]) or ast.unparse(n.targets[0]).startswith("(OK, WARN"))):
        exec(compile(ast.Module([n], []), "lr", "exec"), ns)
tc = ns["terms_cell"]
check("review cell: accepted / not accepted (blocks, says why and what to do) / not checked (warns only)",
      tc(True)["state"] == ns["OK"] and tc(False)["state"] == ns["BAD"] and "Accept" in tc(False)["hint"] and tc(None)["state"] == ns["WARN"])
check("an account without the terms is blocked in Review — before anything is created",
      ns["verdict"]({"terms": tc(False), "geo": ns["cell"](ns["OK"], "Any")})[0].startswith("TikTok's Lead Generation Terms"))
cp = read("app/routes/campaigns.py")
check("the launch stops an Instant Form launch on such an account before creating anything",
      "if _lt_terms.status(acct.advertiser_id) is False:" in cp and cp.index("if _lt_terms.status(acct.advertiser_id) is False:") < cp.index('trace.inflight("Smart+ campaign")'))
check("routes: read a few at a time; accept only the viewer's accounts, via the cookie session, >5 as a job",
      '@router.post("/campaigns/review/lead-terms.json")' in cp and '@router.post("/campaigns/lead-terms/accept")' in cp
      and "if v and sc.allows(v)" in cp.split('"/campaigns/lead-terms/accept"')[1][:1500] and '"lead_terms_accept"' in cp)
check("the job exists", '@jobs.handler("lead_terms_accept")' in read("app/job_handlers.py") and '"lead_terms_accept"' in read("app/jobs.py"))
js = read("app/static/launch-review.js")
check("Review: a Lead terms column, an explicit confirm naming the terms before anything is signed",
      '["terms", "Lead terms"]' in js and "UI.confirm({ title: \"Accept TikTok's Lead Generation Terms" in js and "lead-gen-terms" in js)
check("nothing signs by itself: accept() is only called from the accept route and its job",
      sum(read(p).count("lead_terms.accept(") for p in ("app/routes/campaigns.py", "app/job_handlers.py", "app/background.py", "app/launch_review.py")) == 2)
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
