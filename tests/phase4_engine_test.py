"""Phase 4b (v148) — engine upgrades.

  * Rules rails: flag-only / dry-run modes, an hourly pause cap, once a day per campaign,
    profit lookbacks (yesterday / 3 d / 7 d), a ROAS floor, "Pause now" on a flag.
  * Past date ranges from the saved day rows (no live report per account per page load).
  * Scheduler: every sweep step timed with its last error; the issue scan every 15 min.
  * Launch again on other accounts from a result (duplicate-to-accounts).
  * Geo: countries an account can't target refused before anything is created; the
    account-default geo retries once when TikTok insists on a location.
"""
import importlib, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
FUT = "from __future__ import annotations\n"
def grab(src, name):
    i = src.index("def " + name + "(")
    ends = [e for e in (src.find(m, i + 1) for m in ("\ndef ", "\n@", "\nclass ", "\n# ----")) if e != -1]
    return src[i:(min(ends) if ends else len(src))]

# =======================================================================================
print("-- rules rails --")
rs = read("app/rules.py")
ns = {}
exec(FUT + grab(rs, "decide") + grab(rs, "losing_sources"), ns)
d = ns["decide"]
check("pause mode pauses until the hourly cap, then holds", d("pause", 3, 10) == "pause" and d("pause", 10, 10) == "held" and d("pause", 99, 0) == "pause")
check("flag-only never pauses; dry run only logs", d("flag", 0, 10) == "flag" and d("dry_run", 0, 10) == "would_pause")
ls = ns["losing_sources"]
pnl = {"a": {"spend": 100, "revenue": 60, "profit": -40}, "b": {"spend": 100, "revenue": 70, "profit": -30},
       "c": {"spend": 5, "revenue": 0, "profit": -5}, "d": {"spend": 100, "revenue": 150, "profit": 50}}
check("loss limit: sources losing more than it, past min spend", set(ls(pnl, 10, 35, 0)) == {"a"})
check("ROAS floor catches the slow losers too (and says which rule)", set(ls(pnl, 10, 35, 0.8)) == {"a", "b"} and "ROAS 0.70 < 0.80" in ls(pnl, 10, 35, 0.8)["b"])
check("under min spend nothing is judged", "c" not in ls(pnl, 10, 1, 5))
check("lookback windows in business days", '"yesterday": (-1, 0), "3d": (-2, 1), "7d": (-6, 1)' in rs)
check("both rule kinds go through the rails (one pass object per user)", rs.count("run = _Pass(db, settings, ids)") == 2
      and "_pause_campaign(db, accounts, rec, rule, value, actions, run)" in rs)
rp = grab(rs, "_recently_paused")
check("once a day per campaign, and never within 6 h of a resume; a FAILED pause is retried",
      "local_midnight_utc(0)" in rp and "RESUME_COOLDOWN" in rp and '(last.action != "pause" or last.ok)' in rp)
check("one notice when the cap holds pauses back", "if run.held == 1:" in rs)
st = read("app/settings_store.py")
check("settings: mode, cap, lookback, ROAS floor — validated on save",
      all(k in st for k in ('"rules_mode": "pause"', '"rules_hourly_cap": 10', '"profit_lookback": "today"', '"profit_roas_min": 0.0'))
      and 'clean.get("rules_mode") not in ("pause", "flag", "dry_run")' in st)
sh = read("app/templates/settings.html"); mh = read("app/templates/monitor.html")
check("Settings › Rules has the new controls", 'name="rules_mode"' in sh and 'name="rules_hourly_cap"' in sh and 'name="profit_lookback"' in sh and 'name="profit_roas_min"' in sh)
check("Automation lists flagged / dry-run / held with a 'Pause now'", "'would_pause': 'dry run'" in mh and "/automation/pause" in mh
      and '@router.post("/automation/pause")' in read("app/routes/automation.py"))

# =======================================================================================
print("-- past ranges from the database --")
rdb = importlib.import_module("app.range_db")
m = rdb.combine([("c1", 10.0, 2), ("c1", 5.0, 1), ("c2", 3.0, 0)],
                [("c1", 15.0, 3000, 30, 3), ("c3", 4.0, 1000, 8, 0)])
check("spend + conversions from campaign day rows, impressions + clicks from ad-group rows",
      m["c1"]["spend"] == 15.0 and m["c1"]["conversions"] == 3 and m["c1"]["impressions"] == 3000 and m["c1"]["clicks"] == 30)
check("derived: CTR / CPC / CPM / CPA", (m["c1"]["ctr"], m["c1"]["cpc"], m["c1"]["cpm"], m["c1"]["cpa"]) == (1.0, 0.5, 5.0, 5.0))
check("a campaign with only ad-group rows still gets its spend", m["c3"]["spend"] == 4.0 and m["c3"]["cpa"] == 0.0)
check("no clicks / impressions → zeros, never a division error", m["c2"]["cpc"] == 0.0 and m["c2"]["cpm"] == 0.0)
ss = read("app/routes/status.py")
check("the Campaigns page reads saved history; only uncovered accounts are asked live; ?live=1 forces it",
      "range_db.metrics(db, models, list(in_view), s_day, e_day)" in ss and "for aid in in_view - covered:" in ss
      and 'request.query_params.get("live") != "1"' in ss and "from saved history" in read("app/templates/status.html"))

# =======================================================================================
print("-- scheduler --")
sched = importlib.import_module("app.sched")
sched.begin_sweep(7, True, 60, now=1000.0)
sched.step("sync_campaigns", now=1000.0)
sched.step("balances", now=1012.5)
sched.fail("balances", RuntimeError("wallet API down"))
sched.end_sweep(now=1015.0)
snap = sched.snapshot(now=1020.0)
by = {s["name"]: s for s in snap["steps"]}
check("each step is timed (the next step closes the last)", by["sync_campaigns"]["dur"] == 12.5 and by["balances"]["dur"] == 2.5 and by["sync_campaigns"]["runs"] == 1)
check("a step keeps its last error", "wallet API down" in by["balances"]["error"])
check("the sweep's number, kind and length", snap["sweep"]["n"] == 7 and snap["sweep"]["slow"] and snap["sweep"]["dur"] == 15.0 and snap["since_sweep"] == 5)
check("due(): runs once, then waits its interval", sched.due("x", 900, now=5000) and not sched.due("x", 900, now=5500) and sched.due("x", 900, now=5901))
bg = read("app/background.py")
check("background: beat() times steps; the issue scan waits 15 min; failures recorded",
      "_sched.step(step)" in bg and '_sched.due("issues.scan", ISSUE_SCAN_EVERY_S)' in bg and "ISSUE_SCAN_EVERY_S = 15 * 60" in bg
      and "_sched.end_sweep()" in bg and "_sched2.fail(None, _e)" in bg)
check("Diagnostics shows the scheduler with job lanes", 'id="scheduler"' in read("app/templates/diagnostics.html") and "sched.snapshot()" in read("app/routes/diagnostics.py"))

# =======================================================================================
print("-- launch again on other accounts --")
cp = read("app/routes/campaigns.py")
ns = {}
exec(FUT + grab(cp, "spread"), ns)
check("posts / creatives are spread over the new accounts in turn", ns["spread"]([1, 2], ["a", "b", "c"]) == [["a", 1], ["b", 2], ["c", 1]] and ns["spread"]([], ["a"]) == [])
dup = grab(cp, "duplicate_batch")
check("the same recipe, only accounts in view, never a resume plan carried over, audited",
      "sc.allows(a.advertiser_id)" in dup and 'fields.pop("_resume_by_account", None)' in dup and '"launch.duplicated"' in dup
      and "queue_launch(db, title, ids, fields, pairs=lib_pairs or None, spark_pairs=spark_pairs or None)" in dup)
lr = read("app/templates/launch_result.html")
check("result page: 'Launch again on…' opens the account picker (the batch's accounts excluded)", 'id="dupBtn"' in lr and "exclude: done" in lr and "/duplicate" in lr)

# =======================================================================================
print("-- geo --")
gf = importlib.import_module("app.geo_fit")
check("an account that can't target a wanted country is refused, with which", gf.fit(["6252001", "2635167"], ["3017382"]) == (False, ["3017382"]))
check("targetable → fine; unknown targetable list → never blocks", gf.fit(["6252001"], ["6252001"]) == (True, []) and gf.fit(None, ["x"]) == (True, []))
rows = [types.SimpleNamespace(region_id="6252001", level="COUNTRY", parent_id=""), types.SimpleNamespace(region_id="5332921", level="PROVINCE", parent_id="6252001")]
class Q:
    def __init__(s, r): s.r = r
    def __iter__(s): return iter(s.r)
class RN:
    region_id = level = parent_id = None
db = types.SimpleNamespace(query=lambda *a: Q(rows))
gf._RES.update(at=0.0, rows=None)
co = gf.country_resolver(db, types.SimpleNamespace(RegionName=RN))
check("a province is checked as its country; an unknown id isn't checked", co("5332921") == "6252001" and co("999") is None
      and gf.fit(["2635167"], ["5332921"], co) == (False, ["6252001"]) and gf.fit(["2635167"], ["999"], co) == (True, []))
check("geo fit runs before anything is created (preset countries only; account-default geo skips it)",
      cp.index("geo_fit.fit(") < cp.index("tiktok_api.create_campaign(") and 'raise ConfigError(f"This ad account can\'t target' in cp)
check("account-default geo: unresolved → no location sent, TikTok's refusal → looked up again and retried once",
      '"_geo_unresolved": True' in cp and 'base_payload.pop("location_ids", None)' in cp
      and 'fields.get("_geo_unresolved") and "location_ids" not in ag_payload' in cp and 'ag_payload = {**ag_payload, "location_ids": [loc]}' in cp)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
