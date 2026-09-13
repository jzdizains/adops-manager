"""Ad groups: active only (v106). Runs without sqlalchemy (stubbed).

- status_now(): the latest day's status wins per ad group
- active_metrics(): sums ACTIVE ad groups, reports what was hidden, derives rates
- active_revenue(): postbacks by ad group id → the owning campaign, only when active;
  postbacks without an id are "unsplit" for the source, never dropped
- sync_account(): pages the ad group list, upserts one row per ad group per day,
  survives a failed report call
- the Campaigns page / postback receiver / settings wiring (source checks)
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
    def __ge__(self, o): return ("ge", o)
    def __le__(self, o): return ("le", o)
    def __lt__(self, o): return ("lt", o)
    def in_(self, v): return ("in", list(v))
    __hash__ = object.__hash__
class _F:
    def sum(self, c): return ("sum", c)
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m; return m
sa = _mod("sqlalchemy", func=_F()); _mod("sqlalchemy.orm", Session=type("Session", (), {})); sa.orm = sys.modules["sqlalchemy.orm"]
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
class AdgroupSnapshot:
    advertiser_id = _Col(); campaign_id = _Col(); adgroup_id = _Col(); day = _Col(); operation_status = _Col()
    created_time = _Col(); adgroup_name = _Col(); spend = _Col(); impressions = _Col(); clicks = _Col(); conversions = _Col()
    def __init__(self, **k): self.__dict__.update({"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0, "adgroup_name": "", "operation_status": "", "created_time": ""}); self.__dict__.update(k)
class PostbackEvent:
    source = _Col(); adgroup_id = _Col(); revenue = _Col(); conversions = _Col(); created_at = _Col()
class AdAccount: pass
_mod("app.models", AdgroupSnapshot=AdgroupSnapshot, PostbackEvent=PostbackEvent, AdAccount=AdAccount)
tk = _mod("app.tiktok_api", TikTokError=type("TikTokError", (Exception,), {}), list_adgroups=lambda *a, **k: {"list": []}, get_report=lambda *a, **k: [])
import importlib
ag = importlib.import_module("app.adgroup_stats")

# a fake DB answering the shapes adgroup_stats uses: rows keyed by the columns selected
class DB:
    def __init__(self, snaps=None, pbs=None): self.snaps = snaps or []; self.pbs = pbs or []; self.added = []
    def add(self, r): self.added.append(r)
    def query(self, *cols):
        db = self
        class Q:
            def __init__(self): self.cols = cols; self.conds = []
            def filter(self, *a): self.conds.extend(a); return self
            def group_by(self, *a): return self
            def delete(self, **k): return 0
            def _rows(self):
                first = cols[0]
                if first is AdgroupSnapshot or first is AdgroupSnapshot.campaign_id or first is AdgroupSnapshot.adgroup_id:
                    rows = db.snaps
                    for c in self.conds:
                        if c[0] == "in": rows = [r for r in rows if r.campaign_id in c[1]]
                        if c[0] == "eq": rows = [r for r in rows if r.campaign_id == c[1] or r.advertiser_id == c[1] or r.day == c[1]]
                        if c[0] == "ge": rows = [r for r in rows if r.day >= c[1]]
                        if c[0] == "le": rows = [r for r in rows if r.day <= c[1]]
                    return rows
                rows = db.pbs
                for c in self.conds:
                    if c[0] == "in": rows = [r for r in rows if r["source"] in c[1]]
                    if c[0] == "eq": rows = [r for r in rows if r["source"] == c[1]]
                return rows
            def __iter__(self):
                rows = self._rows()
                names = [getattr(c, "_n", None) for c in cols]
                # figure out the projection by which column objects were passed
                if cols[0] is AdgroupSnapshot:
                    return iter(rows)
                if cols[0] is AdgroupSnapshot.campaign_id and len(cols) == 4:       # status_now
                    return iter([(r.campaign_id, r.adgroup_id, r.day, r.operation_status) for r in rows])
                if cols[0] is AdgroupSnapshot.campaign_id and len(cols) == 6:       # active_metrics group by
                    agg = {}
                    for r in rows:
                        k = (r.campaign_id, r.adgroup_id); a = agg.setdefault(k, [0.0, 0, 0, 0])
                        a[0] += r.spend; a[1] += r.impressions; a[2] += r.clicks; a[3] += r.conversions
                    return iter([(k[0], k[1], *v) for k, v in agg.items()])
                if cols[0] is AdgroupSnapshot.adgroup_id and len(cols) == 5 and cols[1] is AdgroupSnapshot.day:   # drawer latest
                    return iter([(r.adgroup_id, r.day, r.operation_status, r.created_time, r.adgroup_name) for r in rows])
                if cols[0] is AdgroupSnapshot.adgroup_id and len(cols) == 5:       # drawer sums
                    agg = {}
                    for r in rows:
                        a = agg.setdefault(r.adgroup_id, [0.0, 0, 0, 0]); a[0] += r.spend; a[1] += r.impressions; a[2] += r.clicks; a[3] += r.conversions
                    return iter([(k, *v) for k, v in agg.items()])
                if cols[0] is PostbackEvent.source:                                  # active_revenue
                    agg = {}
                    for r in rows:
                        a = agg.setdefault((r["source"], r["adgroup_id"]), [0.0, 0]); a[0] += r["revenue"]; a[1] += r["conversions"]
                    return iter([(k[0], k[1], *v) for k, v in agg.items()])
                if cols[0] is PostbackEvent.adgroup_id:                              # revenue_by_adgroup
                    agg = {}
                    for r in rows:
                        a = agg.setdefault(r["adgroup_id"], [0.0, 0]); a[0] += r["revenue"]; a[1] += r["conversions"]
                    return iter([(k, *v) for k, v in agg.items()])
                raise AssertionError("unexpected query shape %r" % (cols,))
        return Q()

S = AdgroupSnapshot
snaps = [
    S(advertiser_id="a", campaign_id="c1", adgroup_id="g_old", day="2026-09-13", operation_status="ENABLE", spend=3.0, clicks=30, impressions=1000, conversions=2),
    S(advertiser_id="a", campaign_id="c1", adgroup_id="g_old", day="2026-09-14", operation_status="DISABLE", spend=1.0, clicks=10, impressions=300, conversions=1),   # paused later that day
    S(advertiser_id="a", campaign_id="c1", adgroup_id="g_new", day="2026-09-14", operation_status="ENABLE", spend=2.0, clicks=40, impressions=800, conversions=2),
    S(advertiser_id="a", campaign_id="c2", adgroup_id="g_c2", day="2026-09-14", operation_status="ENABLE", spend=5.0, clicks=50, impressions=2000, conversions=0),
]
db = DB(snaps)
st = ag.status_now(db, ["c1", "c2", "c9"])
check("status: the latest day wins (g_old is DISABLE now)", st["c1"]["g_old"] == "DISABLE" and st["c1"]["g_new"] == "ENABLE" and st["c2"]["g_c2"] == "ENABLE" and "c9" not in st, str(st))

m = ag.active_metrics(db, ["c1", "c2", "c9"], "2026-09-13", "2026-09-14")
c1 = m["c1"]
check("active only: c1 = the new ad group alone", c1["spend"] == 2.0 and c1["clicks"] == 40 and c1["conversions"] == 2, str(c1))
check("…and the paused ad group's whole range is hidden, both days", c1["hidden_spend"] == 4.0 and c1["hidden_conversions"] == 3, str(c1))
check("counts: 1 of 2 active", c1["active_n"] == 1 and c1["total_n"] == 2 and c1["has_rows"])
check("rates derived from the sums", round(c1["ctr"], 2) == 5.0 and c1["cpc"] == 0.05 and c1["cpm"] == 2.5 and c1["cpa"] == 1.0, str(c1))
check("a campaign with no ad-group rows: zeros, has_rows False", m["c9"]["has_rows"] is False and m["c9"]["spend"] == 0.0 and m["c9"]["cpa"] == 0.0)
check("c2: fully active, nothing hidden", m["c2"]["spend"] == 5.0 and m["c2"]["hidden_spend"] == 0.0 and m["c2"]["conversions"] == 0 and m["c2"]["cpa"] == 0.0)

pbs = [
    {"source": "Camp1", "adgroup_id": "g_new", "revenue": 5.0, "conversions": 1},
    {"source": "Camp1", "adgroup_id": "g_old", "revenue": 5.0, "conversions": 1},   # paused ad group → not counted
    {"source": "Camp1", "adgroup_id": "", "revenue": 10.0, "conversions": 2},       # before the ClickFlare paste → unsplit
    {"source": "Camp2", "adgroup_id": "g_c2", "revenue": 5.0, "conversions": 1},
    {"source": "Camp1", "adgroup_id": "g_unknown", "revenue": 5.0, "conversions": 1},   # an ad group we never saw → not assigned
]
rv = ag.active_revenue(DB(snaps, pbs), ["c1", "c2"], datetime(2026, 9, 14), datetime(2026, 9, 15), {"c1": "Camp1", "c2": "Camp2"})
check("revenue: only the active ad group's postbacks count", rv["c1"]["revenue"] == 5.0 and rv["c1"]["conversions"] == 1, str(rv))
check("revenue: postbacks with no ad group id are reported as unsplit", rv["c1"]["unsplit_revenue"] == 10.0 and rv["c1"]["unsplit_conversions"] == 2)
check("revenue: c2 gets its own", rv["c2"]["revenue"] == 5.0 and rv["c2"]["unsplit_revenue"] == 0.0)
check("no sources → empty result, no query", ag.active_revenue(DB(snaps, pbs), ["c1"], datetime(2026, 9, 14), datetime(2026, 9, 15), {"c1": ""})["c1"]["revenue"] == 0.0)

d = ag.drawer_rows(DB(snaps), "c1", "2026-09-14", "2026-09-14")
check("drawer: per ad group, today, with the latest status", d["g_old"]["status"] == "DISABLE" and d["g_old"]["spend"] == 1.0 and d["g_new"]["spend"] == 2.0 and d["g_new"]["status"] == "ENABLE", str(d))
r2 = ag.revenue_by_adgroup(DB(snaps, pbs), "Camp1", datetime(2026, 9, 14), datetime(2026, 9, 15))
check("drawer revenue by ad group incl. unsplit under ''", r2["g_new"]["revenue"] == 5.0 and r2[""]["revenue"] == 10.0)

# ---- sync_account ---------------------------------------------------------------
acct = types.SimpleNamespace(access_token="t", advertiser_id="a", advertiser_name="A")
calls = {"pages": []}
def fake_list(token, adv, cids, page=1, page_size=100):
    calls["pages"].append(page)
    lists = {1: [{"adgroup_id": "g1", "campaign_id": "c1", "adgroup_name": "one", "operation_status": "ENABLE", "create_time": "2026-09-14 10:00:00"}],
             2: [{"adgroup_id": "g2", "campaign_id": "c1", "adgroup_name": "two", "operation_status": "DISABLE", "create_time": ""}]}
    return {"list": lists[page], "page_info": {"total_page": 2}}
tk.list_adgroups = fake_list
tk.get_report = lambda *a, **k: [{"dimensions": {"adgroup_id": "g1"}, "metrics": {"spend": "1.5", "clicks": "7", "impressions": "100", "conversion": "1"}}]
sdb = DB()
n = ag.sync_account(sdb, acct, ["c1"], "2026-09-14", ["spend"])
check("sync pages through the ad group list", calls["pages"] == [1, 2] and n == 2)
rows = {r.adgroup_id: r for r in sdb.added}
check("sync upserts one row per ad group with status, name, created time", rows["g1"].operation_status == "ENABLE" and rows["g1"].adgroup_name == "one" and rows["g1"].created_time == "2026-09-14 10:00:00" and rows["g2"].operation_status == "DISABLE")
check("sync fills metrics from the ad-group report; missing → zeros", rows["g1"].spend == 1.5 and rows["g1"].clicks == 7 and rows["g1"].conversions == 1 and rows["g2"].spend == 0.0)
def boom(*a, **k): raise tk.TikTokError("report down")
tk.get_report = boom
sdb2 = DB()
n2 = ag.sync_account(sdb2, acct, ["c1"], "2026-09-14", ["spend"])
check("a failed report still records the ad groups (status matters most)", n2 == 2 and all(r.spend == 0.0 for r in sdb2.added))
tk.list_adgroups = boom
check("a failed list call returns 0, never raises", ag.sync_account(DB(), acct, ["c1"], "2026-09-14", ["spend"]) == 0)
check("no live campaigns → no calls", ag.sync_account(DB(), acct, [], "2026-09-14", ["spend"]) == 0)

# ---- wiring (source) ------------------------------------------------------------
sp = open(os.path.join(ROOT, "app", "routes", "status.py")).read()
check("page reads ?ag=active", 'request.query_params.get(adgroup_stats.MODE_PARAM, "") == "active"' in sp)
check("active mode swaps the metrics and the revenue, no spend-share apportioning", "ag_metrics = adgroup_stats.active_metrics(db, _ids, _s_day, _e_day)" in sp and 'src_pb = {"revenue": ar.get("revenue", 0.0)' in sp and "n = 1" in sp)
check("drawer json carries per-ad-group stats + unsplit", 'g["stats"] = {"spend"' in sp and '"unsplit": {"revenue"' in sp)
tpl = open(os.path.join(ROOT, "app", "templates", "status.html")).read()
check("switch in the filter bar + hidden field + qs carries the mode", 'id="agSeg"' in tpl and 'name="ag"' in tpl and "('&ag=active' if ag_mode else '')" in tpl)
check("row explains what was hidden and unsplit", "from paused ad groups hidden" in tpl and "unsplit revenue" in tpl and "no ad-group data for this range" in tpl)
pb_src = open(os.path.join(ROOT, "app", "routes", "postback.py")).read()
check("postback stores a numeric ad group id, never a literal token", 'q.get("agid") or q.get("adgroup_id")' in pb_src and "not agid_param.isdigit()" in pb_src and "adgroup_id=agid_param," in pb_src)
ss = open(os.path.join(ROOT, "app", "routes", "settings_page.py")).read()
check("postback URL carries &agid={trackingField6}", '"&agid={trackingField" + str(int(s.get("clickflare_agid_field") or 0)) + "}"' in ss)
ls = open(os.path.join(ROOT, "app", "live_spend.py")).read()
check("the sweep syncs ad groups for LIVE campaigns only and can never break the campaign sync", '_ags.sync_account(db, acct, live_ids, today, REPORT_METRICS)' in ls and 'c.get("operation_status") == "ENABLE"' in ls and "except Exception:" in ls)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
