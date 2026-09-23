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
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
