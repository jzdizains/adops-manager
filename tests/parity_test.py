"""Parity watch (v109): an option TikTok uses that the launcher can't set is reported
ONCE as an Inbox notice + Diagnostics entry, with the campaign it was seen on; known
values are silent; nothing here can break the sweep. Runs without sqlalchemy."""
import sys, types, os

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
_mod("sqlalchemy.orm", Session=type("Session", (), {})); _mod("sqlalchemy")
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
class Alert:
    id = object()
    def __init__(self, **k): self.__dict__.update(k)
_mod("app.models", Alert=Alert)
# the launcher's option lists, as parity reads them (subset of the real ones)
_mod("app.routes.launch",
     OPT_GOAL_OPTIONS=[("", "Auto"), ("CONVERT", "Conversions"), ("CLICK", "Clicks"), ("REACH", "Reach"), ("ENGAGED_VIEW", "Engaged views"), ("TRAFFIC_LANDING_PAGE_VIEW", "LPV")],
     GOAL_LABELS={"": "Auto", "CONVERT": "Conversion", "CLICK": "Click", "TRAFFIC_LANDING_PAGE_VIEW": "LPV", "REACH": "Reach", "ENGAGED_VIEW": "Focused"},
     OBJECTIVE_RULES={"TRAFFIC": {"goals": ["CLICK", "TRAFFIC_LANDING_PAGE_VIEW"]}, "WEB_CONVERSIONS": {"goals": ["CONVERT"]}},
     OBJECTIVES=["TRAFFIC", "WEB_CONVERSIONS", "LEAD_GENERATION", "REACH", "VIDEO_VIEWS"],
     PIXEL_EVENTS=[("ON_WEB_DETAIL", "v"), ("FORM", "f"), ("ON_WEB_REGISTER", "r"), ("BUTTON", "b"), ("ON_WEB_ORDER", "o"), ("SHOPPING", "s")],
     PACING_OPTIONS=[("PACING_MODE_SMOOTH", "s"), ("PACING_MODE_FAST", "f")],
     ENGAGED_CANDIDATES=[("ENGAGEMENT_SESSION", "ENGAGED_SESSION", True), ("ENGAGEMENT_SESSION", "", False), ("CONVERT", "ENGAGED_SESSION", True)])
recorded = []
_mod("app.diag", record=lambda kind, where, code, message, context=None, request_id="": recorded.append((kind, where, code, context)))
import importlib
pr = importlib.import_module("app.parity")

class DB:
    def __init__(self): self.added = []; self.existing = set()
    def add(self, r): self.added.append(r)
    def query(self, *a):
        db = self
        class Q:
            def filter_by(self, **k): self.k = k; return self
            def first(self): return object() if self.k.get("ref_id") in db.existing else None
        return Q()

acct = types.SimpleNamespace(advertiser_id="adv1", advertiser_name="Blue Bat")
campaigns = [{"campaign_id": "c1", "campaign_name": "Camp One", "objective_type": "TRAFFIC", "budget_mode": "BUDGET_MODE_DAY"},
             {"campaign_id": "c2", "campaign_name": "Camp Two", "objective_type": "ENGAGEMENT", "budget_mode": "BUDGET_MODE_DAY"}]
adgroups = [{"campaign_id": "c1", "optimization_goal": "CLICK", "billing_event": "CPC", "bid_type": "BID_TYPE_NO_BID", "placements": ["PLACEMENT_TIKTOK"], "pacing": "PACING_MODE_SMOOTH", "optimization_event": "", "promotion_type": "WEBSITE"},
            {"campaign_id": "c1", "optimization_goal": "ENGAGED_SESSION_GOAL", "billing_event": "OCPM", "bid_type": "BID_TYPE_NO_BID", "placements": ["PLACEMENT_TIKTOK", "PLACEMENT_PANGLE"], "optimization_event": "ENGAGED_SESSION"}]
db = DB()
n = pr.observe(db, acct, campaigns, adgroups)
refs = sorted(a.ref_id for a in db.added)
check("only unknown values are reported", refs == ["objective_type:ENGAGEMENT", "optimization_goal:ENGAGED_SESSION_GOAL", "placement:PLACEMENT_PANGLE"], str(refs))
check("a value the launcher probes itself (ENGAGED_SESSION event, v110) is not 'missing'", "optimization_event:ENGAGED_SESSION" not in refs)
check("returns the number of new sightings", n == 3)
check("alerts are Inbox rows of kind parity, level info", all(a.kind == "parity" and a.level == "info" for a in db.added))
msg = next(a.message for a in db.added if a.ref_id == "optimization_goal:ENGAGED_SESSION_GOAL")
check("the notice names the value, the campaign and the account", "ENGAGED_SESSION_GOAL" in msg and "Camp One" in msg and "Blue Bat" in msg, msg)
check("Diagnostics gets the same, with context", any(r[0] == "parity" and r[1] == "optimization_goal" and r[2] == "ENGAGED_SESSION_GOAL" and r[3]["campaign"] == "Camp One" for r in recorded))
# a second sweep with the same values → nothing new (in-process memory)
db2 = DB()
check("the same values are never reported twice in one process", pr.observe(db2, acct, campaigns, adgroups) == 0 and db2.added == [])
# a restart: the Alert row already exists (even acknowledged) → still nothing
pr._seen_this_process.clear()
db3 = DB(); db3.existing = {"objective_type:ENGAGEMENT", "optimization_goal:ENGAGED_SESSION_GOAL", "placement:PLACEMENT_PANGLE"}
check("after a restart, an existing Inbox row (acknowledged or not) suppresses the repeat", pr.observe(db3, acct, campaigns, adgroups) == 0 and db3.added == [])
# no ad groups fetched → campaigns still checked, nothing crashes
pr._seen_this_process.clear()
db4 = DB(); n4 = pr.observe(db4, acct, campaigns, None)
check("without ad groups, campaign-level values are still checked", n4 == 1 and db4.added[0].ref_id == "objective_type:ENGAGEMENT")
# a broken db never raises
class Boom:
    def add(self, r): raise RuntimeError("disk on fire")
    def query(self, *a): raise RuntimeError("disk on fire")
pr._seen_this_process.clear()
check("a failure inside the watch returns 0 instead of breaking the sweep", pr.observe(Boom(), acct, campaigns, adgroups) == 0)
# wiring
ls = open(os.path.join(ROOT, "app", "live_spend.py")).read()
check("the sweep calls the watch with the campaigns and the fetched ad groups", "_parity.observe(db, acct, campaigns, ag_fetched[0] if ag_fetched else None)" in ls)
ib = open(os.path.join(ROOT, "app", "inbox.py")).read()
check("Inbox titles and links the notice", '"parity": "TikTok has an option we don\'t offer"' in ib and '"/diagnostics?kind=parity"' in ib)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
