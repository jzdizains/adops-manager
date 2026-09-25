"""Idle bid bump (v120) + the two one-button clears.

Functional (bid_bump with stubbed models):
- bid_of(): cost cap first, then bid, nothing for lowest cost
- has_schedule(): TikTok's 0/1 half-hour string
- observe(): first sighting starts the clock; growing spend restarts it; unchanged spend
  doesn't; a new local day resets spend without counting as activity; bump counter resets
- due(): every guard in order — rule off, step 0, ad group paused, campaign paused, Smart+,
  not delivering (with TikTok's status in the reason), no bid, dayparting, still inside the
  idle window (from the later of last spend / last bump / first sighting), daily cap, ceiling —
  and the happy path
Static (the shipped files):
- the sweep feeds the watch from the ad-group pull it already makes, schedules per user
- bumps run in a job, one TikTok call per ad group, RuleAction per bump/refusal
- update_adgroup can set bid_price; the five settings are per user and clamped
- Settings card, Health switch, Jobs "Clear finished", Appeals "Clear all" / "Clear history"
Runs without any dependency."""
import os, sys, types
from datetime import datetime, timedelta

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

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
_mod("sqlalchemy.orm", Session=object)

class Col:
    def __init__(self, n): self.n = n
    def in_(self, v): return ("in", self.n, set(v))
    def __eq__(self, o): return ("eq", self.n, o)
    def __ge__(self, o): return ("ge", self.n, o)
    def __lt__(self, o): return ("lt", self.n, o)
    __hash__ = object.__hash__
class Watch:
    adgroup_id = Col("adgroup_id"); operation_status = Col("operation_status"); updated_at = Col("updated_at"); advertiser_id = Col("advertiser_id")
    _seq = 0
    def __init__(self, **kw):
        Watch._seq += 1; self.id = Watch._seq
        d = dict(advertiser_id="", campaign_id="", adgroup_id="", adgroup_name="", day="", spend_seen=0.0, spend_changed_at=None, last_bump_at=None,
                 bumps_today=0, bumps_day="", bid_field="", bid_now=0.0, operation_status="", secondary_status="", dayparting="", skip_reason="", updated_at=None)
        d.update(kw); self.__dict__.update(d)
class CampaignRecord:
    campaign_id = Col("campaign_id")
models = _mod("app.models", AdgroupBidWatch=Watch, CampaignRecord=CampaignRecord)

class Q:
    def __init__(self, rows): self.rows = list(rows)
    def filter(self, *conds):
        rows = self.rows
        for c in conds:
            k, n, v = c
            if k == "in": rows = [r for r in rows if getattr(r, n) in v]
            elif k == "eq": rows = [r for r in rows if getattr(r, n) == v]
            elif k == "ge": rows = [r for r in rows if getattr(r, n) >= v]
            elif k == "lt": rows = [r for r in rows if getattr(r, n) < v]
        return Q(rows)
    def all(self): return list(self.rows)
class DB:
    def __init__(self): self.rows = []
    def query(self, m): return Q(self.rows)
    def add(self, r): self.rows.append(r)
    def commit(self): pass

import importlib
bb = importlib.import_module("app.bid_bump")
T0 = datetime(2026, 9, 16, 10, 0, 0)
M = lambda n: timedelta(minutes=n)

print("\n-- bid_of / has_schedule --")
check("cost cap wins", bb.bid_of({"conversion_bid_price": "0.50", "bid_price": "0.20"}) == ("conversion_bid_price", 0.5))
check("bid when no cap", bb.bid_of({"conversion_bid_price": 0, "bid_price": "0.20"}) == ("bid_price", 0.2))
check("lowest cost has nothing to raise", bb.bid_of({"bid_type": "BID_TYPE_NO_BID"}) == ("", 0.0))
check("all-ones / empty dayparting = all hours", not bb.has_schedule("") and not bb.has_schedule("1" * 336))
check("a 0 anywhere = a schedule", bb.has_schedule("1" * 300 + "0" * 36))

print("\n-- observe --")
db = DB()
g = {"adgroup_id": "ag1", "campaign_id": "c1", "adgroup_name": "AG one", "operation_status": "ENABLE",
     "secondary_status": "ADGROUP_STATUS_DELIVERY_OK", "conversion_bid_price": "0.40", "dayparting": ""}
bb.observe(db, "adv1", [g], {"ag1": {"spend": "0"}}, "2026-09-16", now=T0)
w = db.rows[0]
check("first sighting starts the clock at now, bid + status captured", w.spend_changed_at == T0 and w.bid_field == "conversion_bid_price" and w.bid_now == 0.4 and w.secondary_status == "ADGROUP_STATUS_DELIVERY_OK" and w.campaign_id == "c1")
bb.observe(db, "adv1", [g], {"ag1": {"spend": "0"}}, "2026-09-16", now=T0 + M(30))
check("unchanged spend leaves the clock alone", w.spend_changed_at == T0 and len(db.rows) == 1)
bb.observe(db, "adv1", [g], {"ag1": {"spend": "1.25"}}, "2026-09-16", now=T0 + M(40))
check("spend grew → clock restarts", w.spend_changed_at == T0 + M(40) and w.spend_seen == 1.25)
w.bumps_today = 3
bb.observe(db, "adv1", [g], {"ag1": {"spend": "0"}}, "2026-09-17", now=T0 + M(900))
check("new local day: spend resets without counting as activity; bump counter resets", w.spend_changed_at == T0 + M(40) and w.spend_seen == 0 and w.day == "2026-09-17" and w.bumps_today == 0 and w.bumps_day == "2026-09-17")
bb.observe(db, "adv1", [g], {"ag1": {"spend": "0.30"}}, "2026-09-18", now=T0 + M(2000))
check("new day with spend already on it counts as activity", w.spend_changed_at == T0 + M(2000))

print("\n-- due --")
S = {"bid_bump_enabled": True, "bid_bump_step": 0.05, "bid_bump_idle_min": 60, "bid_bump_ceiling": 0.0, "bid_bump_max_per_day": 6}
def W(**kw):
    d = dict(operation_status="ENABLE", secondary_status="ADGROUP_STATUS_DELIVERY_OK", bid_field="conversion_bid_price", bid_now=0.40,
             dayparting="", spend_changed_at=T0 - M(90), last_bump_at=None, bumps_today=0, spend_seen=2.0)
    d.update(kw); return Watch(**d)
check("happy path: delivering, has a bid, idle 90 min", bb.due(W(), S, T0, True) == (True, "idle 90 min"))
check("rule off", bb.due(W(), {**S, "bid_bump_enabled": False}, T0, True) == (False, "rule off"))
check("step 0", bb.due(W(), {**S, "bid_bump_step": 0}, T0, True)[1] == "step is 0")
check("ad group paused", bb.due(W(operation_status="DISABLE"), S, T0, True)[1] == "ad group paused")
check("campaign paused", bb.due(W(), S, T0, False)[1] == "campaign paused")
check("Smart+ skipped", "Smart+" in bb.due(W(), S, T0, True, smart_plus=True)[1])
check("in review → not delivering, TikTok's status in the reason", bb.due(W(secondary_status="ADGROUP_STATUS_AUDIT"), S, T0, True) == (False, "not delivering: audit"))
check("out of budget → not delivering", bb.due(W(secondary_status="ADGROUP_STATUS_BUDGET_EXCEED"), S, T0, True)[1] == "not delivering: budget exceed")
check("no bid to raise", bb.due(W(bid_field="", bid_now=0), S, T0, True)[1] == "no bid to raise (lowest cost)")
check("dayparting schedule skipped", bb.due(W(dayparting="1" * 100 + "0" * 236), S, T0, True)[1] == "has a dayparting schedule")
check("spent 20 min ago → wait", bb.due(W(spend_changed_at=T0 - M(20)), S, T0, True) == (False, "spent 20 min ago"))
check("fresh ad group, never spent → waits a full idle period from first sighting", bb.due(W(spend_changed_at=T0 - M(20), spend_seen=0), S, T0, True) == (False, "watched 20 of 60 min"))
check("a bump 30 min ago restarts the clock even if the last spend was long ago", bb.due(W(last_bump_at=T0 - M(30)), S, T0, True) == (False, "spent 30 min ago"))
check("daily cap", bb.due(W(bumps_today=6), S, T0, True)[1] == "6 bumps today already (daily cap)")
check("ceiling: 0.40 + 0.05 > 0.42 → skipped", bb.due(W(), {**S, "bid_bump_ceiling": 0.42}, T0, True)[1] == "at the $0.42 ceiling")
check("ceiling: exactly reachable is fine", bb.due(W(), {**S, "bid_bump_ceiling": 0.45}, T0, True)[0] is True)
check("no ceiling when 0", bb.due(W(bid_now=9.0), S, T0, True)[0] is True)

print("\n-- evaluate --")
db = DB()
c_on = types.SimpleNamespace(campaign_id="c1", operation_status="ENABLE", is_smart_plus=False)
db.rows = [W(id=1, campaign_id="c1", advertiser_id="a1", updated_at=T0), W(id=2, campaign_id="c1", advertiser_id="a2", updated_at=T0),
           W(id=3, campaign_id="c1", advertiser_id="a1", updated_at=T0, spend_changed_at=T0 - M(5)), W(id=4, campaign_id="c1", advertiser_id="a1", updated_at=T0 - M(30))]
class DB2(DB):
    def query(self, m):
        return Q([c_on]) if m is CampaignRecord else Q(self.rows)
d2 = DB2(); d2.rows = db.rows
got = bb.evaluate(d2, S, {"a1"}, now=T0)
check("only this user's accounts, only rows the sweep saw recently, only the idle ones", got == [d2.rows[0].id])
check("skip reason recorded on the one still inside the window", d2.rows[2].skip_reason == "spent 5 min ago" and d2.rows[0].skip_reason == "")
check("empty account set → nothing (a user with no accounts)", bb.evaluate(d2, S, set(), now=T0) == [])

print("\n-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
check("the sweep's ad-group pull feeds the watch (no extra TikTok calls)", "bid_bump.observe(db, acct.advertiser_id, groups, metrics, day)" in read("app/adgroup_stats.py"))
bg = read("app/background.py")
check("scheduled per user in the fast pass, pruned in the slow pass", "bid_bump.schedule(db, us, ids, u.id)" in bg and "bid_bump.prune(db)" in bg)
check("bumps run as a job with a stop check, one commit per ad group", '@jobs.handler("bid_bump")' in read("app/job_handlers.py") and "should_stop=lambda: jobs.should_stop(db, job)" in read("app/job_handlers.py")
      and read("app/bid_bump.py").count("db.commit()") >= 3 and "tiktok_api.update_adgroup(token, w.advertiser_id, w.adgroup_id, **{w.bid_field: new})" in read("app/bid_bump.py"))
check("every bump and refusal is a RuleAction", read("app/bid_bump.py").count("models.RuleAction(") == 2)
check("update_adgroup can set bid_price", "bid_price: float | None = None" in read("app/tiktok_api.py") and 'payload["bid_price"] = round(float(bid_price), 2)' in read("app/tiktok_api.py"))
ss = read("app/settings_store.py")
check("five per-user settings, clamped", all(k in ss for k in ("bid_bump_enabled", "bid_bump_step", "bid_bump_idle_min", "bid_bump_ceiling", "bid_bump_max_per_day"))
      and 'clean["bid_bump_idle_min"] = max(int(clean.get("bid_bump_idle_min") or 0), 15)' in ss and "bid_bump" not in str(sorted(k for k in ss.split("GLOBAL_KEYS = frozenset({")[1].split("})")[0].split(","))))
check("model exists", "class AdgroupBidWatch(Base)" in read("app/models.py") and 'adgroup_id = Column(String, unique=True, nullable=False)' in read("app/models.py"))
th = read("app/templates/settings.html")
check("Settings card with the five fields under Rules", all(f'name="{k}"' in th for k in ("bid_bump_enabled", "bid_bump_step", "bid_bump_idle_min", "bid_bump_ceiling", "bid_bump_max_per_day")) and 'bidbump: "rules"' in th)
check("Health lists the switch", '"key": "bidbump"' in read("app/routes/monitor.py"))
check("Jobs: one-button Clear finished (route + button)", '@router.post("/jobs/clear-finished")' in read("app/routes/jobs_page.py") and "def clear_finished" in read("app/jobs.py") and 'id="jbClearDone"' in read("app/templates/jobs.html"))
ap = read("app/routes/appeals_page.py"); at = read("app/templates/appeals.html")
check("Appeals: Clear all dismisses the open rows in view + filter", '@router.post("/appeals/clear")' in ap and "sc.allows(r.advertiser_id)" in ap.split('@router.post("/appeals/clear")')[1] and 'action="/appeals/clear"' in at and "data-confirm=" in at.split('action="/appeals/clear"')[1][:200])
check("Appeals: Clear history removes finished rows", '@router.post("/appeals/clear-history")' in ap and 'action="/appeals/clear-history"' in at)
check("STATIC_VERSION bumped", 'STATIC_VERSION = "176"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
