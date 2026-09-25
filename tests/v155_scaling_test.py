"""v155 — Scaling recommendations (lifetime numbers), Scale ×N now / once approved, and the
OPTIONAL auto-promote (off by default)."""
import importlib, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

sc = importlib.import_module("app.scaling")

print("-- thresholds --")
th = sc.thresholds({})
check("defaults: $50, 1.5×, 5 copies", th == {"min_spend": 50.0, "min_roas": 1.5, "copies": 5})
check("bad values fall back, never crash", sc.thresholds({"scale_min_spend": "x", "scale_min_roas": "", "scale_copies": None}) == th)
check("custom values", sc.thresholds({"scale_min_spend": "100", "scale_min_roas": 2, "scale_copies": 3}) == {"min_spend": 100.0, "min_roas": 2.0, "copies": 3})

print("-- advise (lifetime, never the date picker) --")
win = {"has": True, "spend": 80.0, "roas": 1.9}
check("a delivering, ON winner gets Scale + Expand", sc.advise(win, "active", "ENABLE", th) == ["scale", "expand"])
check("spend under the floor: nothing (ROAS on $2 is luck)", sc.advise({"has": True, "spend": 12, "roas": 4}, "active", "ENABLE", th) == [])
check("ROAS under the bar: nothing", sc.advise({"has": True, "spend": 500, "roas": 1.2}, "active", "ENABLE", th) == [])
check("paused or not on the Active tab: nothing",
      sc.advise(win, "active", "DISABLE", th) == [] and sc.advise(win, "blocked", "ENABLE", th) == [] and sc.advise(win, "pending", "ENABLE", th) == [])
check("no source (no revenue link): nothing", sc.advise({"has": False, "spend": 900, "roas": 0}, "active", "ENABLE", th) == [] and sc.advise(None, "active", "ENABLE", th) == [])
check("the tooltip says why and where the bar lives", "$80 spent at 1.90×" in sc.why(win, th) and "Settings › Scaling" in sc.why(win, th))

print("-- once approved --")
check("delivering → copy it", sc.step("ADGROUP_STATUS_DELIVERY_OK", 1, True) == "fire")
check("still in review → wait", sc.step("ADGROUP_STATUS_NOT_DELIVER_AUDITING", 5, False) == "wait")
check("rejected → cancelled, nothing copied", sc.step("ADGROUP_STATUS_AUDIT_DENY", 1, False) == "rejected")
check("gives up after the TTL", sc.step("ADGROUP_STATUS_NOT_DELIVER_AUDITING", sc.WATCH_TTL_H, False) == "expire")
check("delivering but no source row → wait", sc.step("DELIVERY_OK", 1, False) == "wait")

print("-- auto-promote plan --")
cands = [{"campaign_id": "a", "roas": 1.6, "spend": 90}, {"campaign_id": "b", "roas": 3.0, "spend": 60},
         {"campaign_id": "c", "roas": 2.2, "spend": 300}, {"campaign_id": "d", "roas": 2.2, "spend": 400}]
p = sc.promote_plan(cands, set(), 0, 9, 3)
check("best ROAS first (ties: more spend), stops at the day's cap", [c["campaign_id"] for c in p] == ["b", "d", "c"])
check("a campaign is promoted once, ever", [c["campaign_id"] for c in sc.promote_plan(cands, {"b", "d"}, 0, 12, 3)] == ["c", "a"])
check("today's earlier promotions count against the cap", [c["campaign_id"] for c in sc.promote_plan(cands, set(), 7, 12, 3)] == ["b"])
check("cap reached / cap 0 → nothing", sc.promote_plan(cands, set(), 12, 12, 3) == [] and sc.promote_plan(cands, set(), 0, 0, 3) == [])

print("-- wiring --")
ss = read("app/settings_store.py")
check("auto-promote is OFF by default", '"autoscale_enabled": False' in ss)
check("the thresholds and caps are settings", all(k in ss for k in ('"scale_min_spend": 50.0', '"scale_min_roas": 1.5', '"scale_copies": 5', '"autoscale_copies": 3', '"autoscale_daily_max": 12')))
st = read("app/templates/settings.html")
check("Settings › Scaling card, the switch is a plain opt-in checkbox",
      'id="scaling"' in st and 'name="autoscale_enabled" {{ \'checked\' if s.autoscale_enabled }}' in st
      and all(f'name="{k}"' in st for k in ("scale_min_spend", "scale_min_roas", "scale_copies", "autoscale_copies", "autoscale_daily_max")))
check("…inside the Rules tab (the settings form)", st.index('id="scaling"') < st.index('<section class="stab" data-tab="launch">') and st.index('id="scaling"') > st.index('id="rules"'))
m = read("app/models.py")
check("ScaleWatch table", "class ScaleWatch(Base)" in m and '__tablename__ = "scale_watches"' in m)
bg = read("app/background.py")
check("the sweep acts on waits every time, auto-promote only on the slow tick, errors contained",
      "scaling.tick(db, _m)" in bg and "scaling.auto_promote(db, _m)" in bg and '_sched.fail("scaling"' in bg and 'fromlist=["prune"]).prune(db, models)' in bg)
check("auto-promote only runs for users who switched it on", 'if not us.get("autoscale_enabled"):' in read("app/scaling.py"))
check("…and posts every decision to the Inbox under the ad account", 'kind="rule_action"' in read("app/scaling.py") and 'ref_id=c["advertiser_id"]' in read("app/scaling.py"))
rs = read("app/routes/status.py")
check("scale route: scoped to the viewer's accounts, now or once approved",
      '"/campaigns/{advertiser_id}/{campaign_id}/scale"' in rs and "guard.account_in_view" in rs.split('/scale"')[1][:1500]
      and "delivering_only=False" in rs and "scaling.queue_copies" in rs)
check("cancel route checks the watch belongs to the viewer", '"/scale/watch/{watch_id}/cancel"' in rs and "sc.allows(w.advertiser_id)" in rs)
tpl = read("app/templates/status.html")
check("row chips: Scale / Expand / waiting", 'data-rec="scale"' in tpl and "rec-chip expand" in tpl and "?again=1" in tpl and "rec-chip wait" in tpl)
check("Ready-to-scale filter", 'id="recOnly"' in tpl and "rec_count" in tpl)
js = read("app/static/campaigns.js")
check("popover posts and refreshes in place (no page reload)", "/scale/watch/" in js and "refreshInPlace()" in js and "applyRecOnly" in js)
check("Expand opens 'Launch this batch again'", "again=1" in read("app/templates/launch_result.html") and "dupBtn" in read("app/templates/launch_result.html"))

print("-- Campaigns board (v155) --")
import ast, types
src = read("app/routes/status.py")
tree = ast.parse(src)
ns = {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.Assign)) and getattr(node, "name", None) in ("board_filter", "leaderboard", "objective_label") or \
       (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "OBJECTIVE_LABELS" for t in node.targets)):
        exec(compile(ast.Module([node], []), "status", "exec"), ns)
R = lambda cid, obj="WEB_CONVERSIONS", st="ENABLE", adv="A": types.SimpleNamespace(campaign_id=cid, objective_type=obj, operation_status=st, advertiser_id=adv)
bf = ns["board_filter"]
base = dict(hidden={"2"}, show_hidden=False, obj="", offer="", sources={"1": "src"}, warm=set())
check("hidden campaigns leave the board…", bf(R("1"), **base) and not bf(R("2"), **base))
check("…and 'Hidden' shows only them", not bf(R("1"), **{**base, "show_hidden": True}) and bf(R("2"), **{**base, "show_hidden": True}))
check("objective filter", bf(R("1"), **{**base, "obj": "WEB_CONVERSIONS"}) and not bf(R("1", "TRAFFIC"), **{**base, "obj": "WEB_CONVERSIONS"}))
check("no offer matched = no source", not bf(R("1"), **{**base, "offer": "none"}) and bf(R("3"), **{**base, "offer": "none"}))
check("warm-ups left out when asked", not bf(R("1"), **{**base, "warm": {"1"}}))
check("objective names read like TikTok's", ns["objective_label"]("WEB_CONVERSIONS") == "Website conversions" and ns["objective_label"]("SOME_NEW") == "Some New")
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "row_matches":
        exec(compile(ast.Module([node], []), "status", "exec"), ns)
rm = ns["row_matches"]
check("search (v155.37): name, account, and a campaign id or account id — whole or its tail",
      rm("playful", "USA_Playful_170801", "Faubulous", "1877301201948002", "7461668704875528209") and rm("faub", "x", "Faubulous +5", "1", "2")
      and rm("1877301201948002", "x", "y", "1877301201948002", "7461668704875528209") and rm("8002", "x", "y", "1877301201948002", "7461668704875528209")
      and rm("7461668704875528209", "x", "y", "1877301201948002", "7461668704875528209") and not rm("9999", "x", "y", "1877301201948002", "7461668704875528209")
      and rm("", "x", "y", "1", "2"))
check("…both row loops use it", src.count("row_matches(q, r.campaign_name") == 2)
rows = [{"r": R("1", adv="A"), "m": {"spend": 100}, "revenue": 180}, {"r": R("2", adv="A", st="DISABLE"), "m": {"spend": 50}, "revenue": 20},
        {"r": R("3", adv="B"), "m": {"spend": 10}, "revenue": 5}, {"r": R("4", adv="Z"), "m": {"spend": 1}, "revenue": 0}]
lb = ns["leaderboard"](rows, {"A": 1, "B": 2}, {1: "ana", 2: "ben"})
check("leaderboard: per buyer, best profit first, unassigned accounts kept",
      [x["name"] for x in lb] == ["ana", "Unassigned accounts", "ben"] and lb[0]["spend"] == 150 and lb[0]["profit"] == 50 and lb[0]["roas"] == 1.33
      and lb[0]["n"] == 2 and lb[0]["active"] == 1, lb)
check("both loops (rows and tab counts) use the same filters", src.count("if not board_filter(r, **_bf):") == 2)
check("hide route: only campaigns in the viewer's accounts, per workspace",
      '@router.post("/campaigns/hide")' in src and "if sc.allows(a)" in src.split('/campaigns/hide")')[1][:1200] and "hidden_campaigns:" in src)
check("leaderboard only for the owner on Everyone", "if sc.everything and guard.is_owner(request):" in src)
check("what hidden campaigns spent is still shown under the table", 'recon["hidden"]' in src and "Spend on campaigns you hid" in tpl)
check("board chips + hidden inputs + sort links keep them",
      all(x in tpl for x in ('data-ftog="offer"', 'data-ftog="nowarm"', 'data-ftog="hidden"', 'name="obj"', "'&obj=' ~", 'data-bulk="hide"', 'id="teamBtn"', "team|js")))
check("JS: toggles, hide (menu + bulk), team drawer; counts refresh in place",
      all(x in js for x in ("[data-ftog]", "function hideRows", '"/campaigns/hide"', 'data-act="hide"', "UI.drawer({ title: \"Team\"", 'doc.getElementById("teamData")')))
css = read("app/static/style.css")
check("phone: the status tabs swipe sideways", "scroll-snap-type: x proximity" in css and "#stateSeg" in css.split("v155 board")[1])

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
