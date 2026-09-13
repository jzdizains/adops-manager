"""Lander funnel (v99): the beacon store and the per-source funnel rows.

Runs without sqlalchemy (stubbed): checks validation, the rate cap, macro cleaning,
pruning cadence, and that rows() turns GROUP BY output into steps + rates correctly.
"""
import sys, types, os
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

class _Col:
    def __ge__(self, o): return True
    def __lt__(self, o): return True
    def __eq__(self, o): return True
    __hash__ = object.__hash__
class _F:
    def count(self, *a): return self
    def distinct(self, *a): return self
    def sum(self, *a): return self
sa = types.ModuleType("sqlalchemy"); sa.func = _F(); orm = types.ModuleType("sqlalchemy.orm")
class Session: pass
orm.Session = Session; sa.orm = orm
sys.modules["sqlalchemy"], sys.modules["sqlalchemy.orm"] = sa, orm
pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]; sys.modules["app"] = pkg
models = types.ModuleType("app.models")
class LanderEvent:
    created_at = _Col(); source = _Col(); page = _Col(); step = _Col(); vid = _Col(); id = _Col(); has_ttclid = _Col(); via = _Col()
    def __init__(self, **k): self.__dict__.update(k)
models.LanderEvent = LanderEvent
sys.modules["app.models"] = models

import importlib
fn = importlib.import_module("app.funnel")

class DB:
    def __init__(self, groups=None, arrived=None): self.added = []; self.groups = groups or []; self.arrived = arrived or []; self.deleted = 0
    def add(self, r): self.added.append(r)
    def query(self, *cols):
        db = self
        two = len(cols) == 2          # the "arrived via Continue" query selects (source, count)
        class Q:
            def filter(self, *a): return self
            def group_by(self, *a): return self
            def __iter__(self): return iter(db.arrived if two else db.groups)
            def delete(self, **k): db.deleted += 1; return 0
        return Q()

# ---- accept() ---------------------------------------------------------------
db = DB()
ok, why = fn.accept(db, {"page": "start", "step": "view", "source": "Camp_120000_a1111", "vid": "abc", "ttclid": 1, "cid": "123"})
check("valid beacon stored", ok and len(db.added) == 1, why)
r = db.added[0]
check("fields kept", r.source == "Camp_120000_a1111" and r.page == "start" and r.step == "view" and r.vid == "abc" and r.has_ttclid is True and r.campaign_id == "123")
check("unknown step rejected", fn.accept(db, {"page": "start", "step": "cta"})[0] is False)      # cta is a /play step
check("unknown page rejected", fn.accept(db, {"page": "thanks", "step": "view"})[0] is False)
check("missing everything rejected", fn.accept(db, {})[0] is False)
check("nothing stored for rejected beacons", len(db.added) == 1)
ok, _ = fn.accept(db, {"page": "play", "step": "cta", "source": "{trackingField3}", "vid": "__VID__", "cid": "{campaign_id}"})
check("macro-looking values are blanked, beacon still counted", ok and db.added[-1].source == "" and db.added[-1].vid == "" and db.added[-1].campaign_id == "")
ok, _ = fn.accept(db, {"page": "play", "step": "view", "source": "x" * 500, "vid": "v" * 200})
check("lengths capped", len(db.added[-1].source) == 200 and len(db.added[-1].vid) == 64)

# rate cap: MAX_PER_MINUTE in one minute, then dropped, then a new minute resets
fn._bucket["minute"], fn._bucket["n"] = None, 0
db2 = DB()
for _ in range(fn.MAX_PER_MINUTE):
    fn.accept(db2, {"page": "play", "step": "view"})
ok, why = fn.accept(db2, {"page": "play", "step": "view"})
check(f"beacon {fn.MAX_PER_MINUTE + 1} in a minute is dropped", not ok and why == "rate" and len(db2.added) == fn.MAX_PER_MINUTE)
fn._bucket["minute"] = fn._bucket["minute"] - timedelta(minutes=1)     # pretend the clock rolled
ok, _ = fn.accept(db2, {"page": "play", "step": "view"})
check("a new minute accepts again", ok)

# pruning: at most once an hour
fn._prune_at[0] = datetime.min
db3 = DB()
fn.accept(db3, {"page": "play", "step": "view"}); fn.accept(db3, {"page": "play", "step": "view"})
check("prune ran once for two beacons in the same hour", db3.deleted == 1)

# ---- rows() -----------------------------------------------------------------
groups = [
    ("CampA", "start", "view", 100, 130, 90), ("CampA", "start", "engaged", 80, 80, 0), ("CampA", "start", "continue", 60, 61, 0),
    ("CampA", "play", "view", 50, 55, 0), ("CampA", "play", "engaged", 40, 40, 0), ("CampA", "play", "cta", 10, 12, 0),
    ("CampB", "start", "view", 20, 20, 5), ("", "play", "view", 3, 3, 0),
]
out = fn.rows(DB(groups, arrived=[("CampA", 45)]), datetime(2026, 9, 1), datetime(2026, 9, 2), {"CampA": {"conversions": 2, "revenue": 10.0}, "Ghost": {"conversions": 9}})
a = next(r for r in out if r["source"] == "CampA")
check("distinct visitors per step", (a["start_view"], a["start_engaged"], a["start_continue"], a["play_view"], a["play_engaged"], a["play_cta"]) == (100, 80, 60, 50, 40, 10))
check("hits summed separately", a["hits"] == 130 + 80 + 61 + 55 + 40 + 12)
check("rates are against the ENGAGED count of each page", (a["r_engaged"], a["r_continue"], a["r_play_engaged"], a["r_cta"], a["r_conv"]) == (0.8, 0.75, 0.8, 0.25, 0.2), str({k: a[k] for k in a if k.startswith("r_")}))
check("hand-off: arrived via Continue ÷ presses", a["play_arrived"] == 45 and a["r_arrive"] == 0.75, str(a))
ok, _ = fn.accept(DB(), {"page": "play", "step": "view", "via": "continue"})
ok2, _ = fn.accept(DB(), {"page": "play", "step": "view", "via": "whatever"})
d1 = DB(); fn.accept(d1, {"page": "play", "step": "view", "via": "continue"}); d2 = DB(); fn.accept(d2, {"page": "play", "step": "view", "via": "x"})
check("via is stored only as 'continue' or blank", ok and ok2 and d1.added[0].via == "continue" and d2.added[0].via == "")
check("engaged is an accepted step on both pages", fn.accept(DB(), {"page": "start", "step": "engaged"})[0] and fn.accept(DB(), {"page": "play", "step": "engaged"})[0])
check("ttclid share from /start views", a["with_ttclid"] == 90 and a["r_ttclid"] == 0.9)
check("conversions joined by source", a["conversions"] == 2 and a["revenue"] == 10.0)
b = next(r for r in out if r["source"] == "CampB")
check("missing steps are 0 and rates None", b["start_engaged"] == 0 and b["r_engaged"] == 0 and b["r_continue"] is None and b["r_arrive"] is None and b["play_arrived"] == 0 and b["conversions"] == 0)
check("sorted by /start views, busiest first", [r["source"] for r in out][:2] == ["CampA", "CampB"])
check("no-source beacons kept as their own row", any(r["source"] == "" and r["play_view"] == 3 for r in out))
check("a postback source with no beacons does not invent a row", not any(r["source"] == "Ghost" for r in out))

# ---- rows(source=…): the campaign drawer narrows the query to one source ----------
class DBF(DB):
    def __init__(self, groups): super().__init__(groups); self.filters = 0
    def query(self, *cols):
        db = self
        two = len(cols) == 2
        class Q:
            def filter(self, *a): db.filters += 1; return self
            def group_by(self, *a): return self
            def __iter__(self): return iter([] if two else db.groups)
        return Q()
dbf = DBF([("CampA", "start", "view", 10, 10, 1)])
fn.rows(dbf, datetime(2026, 9, 1), datetime(2026, 9, 2))
n_all = dbf.filters
dbf2 = DBF([("CampA", "start", "view", 10, 10, 1)])
one = fn.rows(dbf2, datetime(2026, 9, 1), datetime(2026, 9, 2), source="CampA")
check("source= adds one more filter to each of the two queries", dbf2.filters == n_all + 2)
check("…and still returns the row", one and one[0]["source"] == "CampA" and one[0]["start_view"] == 10)
sp = open(os.path.join(ROOT, "app", "routes", "status.py")).read()
check("drawer endpoint exists and narrows by source", '"/campaigns/{advertiser_id}/{campaign_id}/funnel.json"' in sp and "funnel.rows(db, s_naive, e_naive, conv, source=src)" in sp)
check("drawer endpoint only accepts known ranges", 'if range_key not in ("today", "yesterday", "7d", "30d"):' in sp)
js = open(os.path.join(ROOT, "app", "static", "campaigns.js")).read()
check("drawer renders the funnel section with a range toggle", 'id="dwFunnel"' in js and 'id="dwFnRange"' in js and 'loadFunnel(adv, cid, "today")' in js)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
