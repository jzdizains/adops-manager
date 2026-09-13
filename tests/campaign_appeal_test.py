"""Campaign page: rejected ad groups + one-click appeal (v101).

- appeals.by_campaign(): per-campaign counts of open / appealing / failed rejections,
  built from one GROUP BY, ignoring rejections that are gone.
- appeals.row_state(): what the drawer renders — can_appeal only for pending/skipped/error.
- appeals.rows_for_campaign(): latest row per ad group.
- appeals.track_adgroup_now(): reads the ad group's ads from TikTok and runs sync for
  just the rejected ones; None when nothing is appealable.
- the endpoint's rules (source check): one appeal per rejection, never twice.

Runs without sqlalchemy (stubbed).
"""
import sys, types, os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

class _Col:
    def __eq__(self, o): return ("eq", o)
    def __ne__(self, o): return True
    def in_(self, v): return ("in", v)
    def desc(self): return self
    def asc(self): return self
    __hash__ = object.__hash__
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m; return m
class _F:
    def count(self, *a): return self
sa = _mod("sqlalchemy", func=_F()); _mod("sqlalchemy.orm", Session=type("Session", (), {})); sa.orm = sys.modules["sqlalchemy.orm"]
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
class Appeal:
    campaign_id = _Col(); status = _Col(); id = _Col(); gone = _Col(); advertiser_id = _Col(); adgroup_id = _Col()
    def __init__(self, **k):
        self.__dict__.update({"reasons": "", "suggestion": "", "ads_n": 1, "ad_name": "", "rejected_status": "",
                              "error": "", "filed_by": "", "submitted_at": None, "adgroup_id": "", "status": "pending"})
        self.__dict__.update(k)
models = _mod("app.models", Appeal=Appeal)
_mod("app.tiktok_api", AD_REJECTED_STATUSES=("AD_STATUS_AUDIT_DENY",), ADGROUP_REJECTED_STATUSES=("AD_STATUS_ADGROUP_AUDIT_DENY",),
     TikTokError=Exception, list_ads=lambda *a, **k: {"list": []})
_mod("app.timeutil", local_midnight_utc=lambda: datetime(2026, 9, 13))
_mod("app.settings_store", get_settings=lambda db: {})
_mod("app.templating", _ago=lambda dt: "2 h ago")

import importlib
ap = importlib.import_module("app.appeals")

# ---- by_campaign -------------------------------------------------------------
class _GroupDB:
    def __init__(self, rows): self.rows = rows; self.filters = []
    def query(self, *a):
        db = self
        class Q:
            def filter(self, *a): db.filters.extend(a); return self
            def group_by(self, *a): return self
            def __iter__(self): return iter(db.rows)
        return Q()
db = _GroupDB([("c1", "pending", 2), ("c1", "error", 1), ("c1", "appealing", 3), ("c2", "failed", 1), ("", "pending", 4)])
out = ap.by_campaign(db)
check("open = pending + skipped + error", out["c1"]["open"] == 3)
check("appealing and failed counted separately", out["c1"]["appealing"] == 3 and out["c2"]["failed"] == 1 and out["c2"]["open"] == 0)
check("rows with no campaign id don't crash", out[""]["open"] == 4)
check("gone rejections are filtered out", any(f == ("eq", False) for f in db.filters))
check("only attention statuses are queried", any(f[0] == "in" and set(f[1]) == set(ap.ATTENTION) for f in db.filters if isinstance(f, tuple)))

# ---- row_state ---------------------------------------------------------------
check("no row → None", ap.row_state(None) is None)
r = Appeal(id=7, status="pending", reasons="Prohibited content: gambling", suggestion="Edit the ad", ads_n=2, ad_name="Ad A; Ad B")
st = ap.row_state(r)
check("pending can be appealed", st["can_appeal"] and st["id"] == 7 and st["label"] == "Not appealed yet")
check("reasons and suggestion carried", st["reasons"].startswith("Prohibited") and st["suggestion"] == "Edit the ad" and st["ads_n"] == 2)
for status, can in (("skipped", True), ("error", True), ("appealing", False), ("failed", False), ("successful", False), ("done", False), ("dismissed", False)):
    check(f"{status}: can_appeal={can}", ap.row_state(Appeal(id=1, status=status))["can_appeal"] is can)
check("filed_ago only when submitted", ap.row_state(Appeal(id=1, status="appealing", submitted_at=datetime(2026, 9, 13)))["filed_ago"] == "2 h ago"
      and ap.row_state(Appeal(id=1, status="pending"))["filed_ago"] == "")
check("long reasons are capped", len(ap.row_state(Appeal(id=1, reasons="x" * 900))["reasons"]) == 400)

# ---- rows_for_campaign: latest per ad group ------------------------------------
class _ListDB:
    def __init__(self, rows): self.rows = rows
    def query(self, *a):
        db = self
        class Q:
            def filter(self, *a): return self
            def order_by(self, *a): return self
            def __iter__(self): return iter(db.rows)
        return Q()
rows = [Appeal(id=1, adgroup_id="g1", status="failed"), Appeal(id=2, adgroup_id="g1", status="pending"), Appeal(id=3, adgroup_id="g2", status="appealing")]
m = ap.rows_for_campaign(_ListDB(rows), "adv", "c1")
check("latest row wins per ad group", m["g1"].id == 2 and m["g2"].id == 3 and len(m) == 2)

# ---- track_adgroup_now ---------------------------------------------------------
calls = {}
tk = sys.modules["app.tiktok_api"]
def fake_sync(db, rejected, scanned_advertisers=None, settings=None):
    calls["rejected"] = rejected; calls["scanned"] = scanned_advertisers
ap.sync = fake_sync
ap._latest_row = lambda db, adv, agid: Appeal(id=99, adgroup_id=agid, status="pending")
acct = types.SimpleNamespace(access_token="tok", advertiser_id="adv1", advertiser_name="Acct")
tk.list_ads = lambda *a, **k: {"list": [{"ad_id": "a1", "adgroup_id": "g1", "secondary_status": "AD_STATUS_DELIVERY_OK"}]}
check("nothing rejected → None, sync not run", ap.track_adgroup_now(None, acct, "c1", "g1") is None and "rejected" not in calls)
tk.list_ads = lambda *a, **k: {"list": [{"ad_id": "a1", "adgroup_id": "g1", "secondary_status": "AD_STATUS_AUDIT_DENY", "campaign_name": "Camp"},
                                        {"ad_id": "a2", "adgroup_id": "g1", "secondary_status": "AD_STATUS_DELIVERY_OK"}]}
row = ap.track_adgroup_now(None, acct, "c1", "g1")
check("rejected ads only, with account + campaign attached", len(calls["rejected"]) == 1 and calls["rejected"][0]["advertiser_id"] == "adv1"
      and calls["rejected"][0]["access_token"] == "tok" and calls["rejected"][0]["campaign_id"] == "c1", str(calls))
check("sync runs WITHOUT the clearing step (no scanned set)", calls["scanned"] is None)
check("returns the row sync created", row is not None and row.id == 99)

# ---- endpoint rules (source) ---------------------------------------------------
src = open(os.path.join(ROOT, "app", "routes", "status.py")).read()
check("appeal endpoint exists", '"/campaigns/{advertiser_id}/{campaign_id}/adgroups/{adgroup_id}/appeal"' in src)
check("already appealing → refused", 'if row.status == "appealing":' in src)
check("finished rejections → refused (one appeal per rejection)", 'if row.status in ("successful", "done", "failed", "dismissed"):' in src)
check("files through the same appeals_file job as the Appeals page", 'jobs_mod.enqueue(db, "appeals_file"' in src)
check("adgroups.json carries the appeal state", 'g["appeal"] = appeals_mod.row_state(tracked.get(g["adgroup_id"]))' in src)
check("campaign rows get the rejection counts", '"rejections": appeals_mod.by_campaign(db)' in src)
tpl = open(os.path.join(ROOT, "app", "templates", "status.html")).read()
check("badge on the campaign row opens the drawer", 'class="pill err open-drawer"' in tpl and "rejected</a>" in tpl)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
