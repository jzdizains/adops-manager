"""Traffic optimisation goal picker — Click / Landing page view / Engaged session (v110).

Ads Manager offers three goals on a Traffic campaign; the preset form now does too.
Click and Landing page view are documented API values; Engaged session's value is
not published, so the launcher probes candidate payloads with TikTok as the judge,
remembers the pair TikTok accepted and tries it first from then on.

Covers, against the shipped code (launch.py imported for real; the payload helpers
extracted from campaigns.py the way identity_bc_test does):
- synthesize(): goal → optimization_goal / billing_event, stale stored values ignored,
  Engaged session takes its pixel from the Traffic block, Click/LPV send none.
- build_adgroup_payload(): cost cap on the right bid field per billing event; pixel only
  on Engaged session; no optimisation event on Traffic.
- build_campaign_payload(): CBO carries the remembered goal, else the first candidate.
- engaged_pairs() / traffic_variants() / campaign_goal_candidates(): order, remembered
  first, CONVERT skipped without a pixel, locked goal after a CBO campaign exists.
- _walkable(): which TikTok complaints move to the next candidate.
- the runner: campaign-level probing + lock + remember-on-first-ad-group (static).
- the form + JS wiring, the parity watch knowing the candidates, the presets list.
Runs without fastapi/sqlalchemy (stubbed)."""
import json, os, re, sys, types
from datetime import datetime, timezone

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
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
class Template:
    def __init__(self, objective_type="TRAFFIC", blob=None, mode="ABO", budget=None):
        self.id, self.name = 1, "T"
        self.objective_type, self.campaign_budget_mode, self.campaign_budget = objective_type, mode, budget
        self.campaign_name_pattern = ""
        self.adgroup_settings = json.dumps(blob or {})
class AdAccount:
    def __init__(self): self.advertiser_id, self.advertiser_name, self.access_token = "7000000000000000001", "Acct", "tok"
_mod("app.models", Template=Template, AdAccount=AdAccount)
_mod("sqlalchemy.orm", Session=object)

import importlib
launch = importlib.import_module("app.routes.launch")

# ---- settings stub -----------------------------------------------------------------
SETTINGS = {}
queries = _mod("app.queries", get_setting=lambda db, k, default="": SETTINGS.get(k, default),
               set_setting=lambda db, k, v: SETTINGS.__setitem__(k, v))
recorded = []
_mod("app.diag", record=lambda kind, where, code, message, context=None, request_id="": recorded.append((code, context)))
class TikTokError(Exception):
    def __init__(self, code=40002, message="boom"):
        super().__init__(f"{code}: {message}"); self.code, self.message = code, message
tiktok_api = _mod("app.tiktok_api", TikTokError=TikTokError)

# ---- the payload helpers, extracted from campaigns.py -------------------------------
src = open(os.path.join(ROOT, "app", "routes", "campaigns.py"), encoding="utf-8").read()
start = src.index("def build_campaign_payload(")
end = src.index("def build_ad_payload(")
helpers = types.ModuleType("app.routes.campaigns")
helpers.__dict__.update({"models": sys.modules["app.models"], "tiktok_api": tiktok_api, "queries": queries,
                         "launch_mod": launch, "Session": object, "datetime": datetime, "timezone": timezone,
                         "_campaign_name": lambda fields, acct: "camp", "__package__": "app.routes",
                         "__name__": "app.routes.campaigns"})
exec(compile(src[start:end], "campaigns-helpers", "exec"), helpers.__dict__)
acct = AdAccount()

# ==== 1 · synthesize ====================================================================
print("\n-- synthesize: the picked goal decides goal + billing --")
f_click = launch.synthesize(Template(blob={"traffic_goal": "CLICK", "traffic_pixel": "C0FFEE"}))
check("Click → CLICK / CPC", (f_click["optimization_goal"], f_click["billing_event"]) == ("CLICK", "CPC"),
      str((f_click["optimization_goal"], f_click["billing_event"])))
check("Click sends no pixel", f_click["pixel_id"] == "" and f_click["pixel_code"] == "")
f_lpv = launch.synthesize(Template(blob={"traffic_goal": "LPV", "traffic_pixel": "C0FFEE", "optimization_goal": "CLICK", "billing_event": "CPC"}))
check("Landing page view → TRAFFIC_LANDING_PAGE_VIEW / OCPM even with a stale stored CLICK/CPC",
      (f_lpv["optimization_goal"], f_lpv["billing_event"]) == ("TRAFFIC_LANDING_PAGE_VIEW", "OCPM"),
      str((f_lpv["optimization_goal"], f_lpv["billing_event"])))
check("Landing page view sends no pixel (Ads Manager shows none)", f_lpv["pixel_id"] == "")
f_eng = launch.synthesize(Template(blob={"traffic_goal": "ENGAGED", "traffic_pixel": "C0FFEE", "optimization_event": "SHOPPING", "pixel_id": "999"}))
check("Engaged session → first candidate / OCPM", (f_eng["optimization_goal"], f_eng["billing_event"]) == (launch.ENGAGED_CANDIDATES[0][0], "OCPM"))
check("Engaged session takes its pixel from the Traffic block, not the hidden conversion field", f_eng["pixel_id"] == "C0FFEE")
check("Traffic never carries a pixel event", f_eng["optimization_event"] == "" and f_lpv["optimization_event"] == "")
f_none = launch.synthesize(Template(blob={}))
check("no goal stored → Click (old presets keep behaving)", f_none["traffic_goal"] == "CLICK" and f_none["optimization_goal"] == "CLICK")
f_conv = launch.synthesize(Template(objective_type="WEB_CONVERSIONS", blob={"traffic_goal": "ENGAGED", "pixel_id": "999", "optimization_event": "SHOPPING"}))
check("a conversion preset is untouched by the traffic goal", f_conv["traffic_goal"] == "" and f_conv["pixel_id"] == "999"
      and f_conv["optimization_event"] == "SHOPPING" and f_conv["optimization_goal"] == "CONVERT", str(f_conv["optimization_goal"]))
check("the three options are exactly Ads Manager's", [k for k, _, _ in launch.TRAFFIC_GOAL_OPTIONS] == ["CLICK", "LPV", "ENGAGED"])

# ==== 2 · ad-group payload =============================================================
print("\n-- build_adgroup_payload --")
def ag(fields, bid=None, pixel=""):
    return helpers.build_adgroup_payload(fields, acct, "1", 0, bid, pixel)
p = ag(f_click, bid=0.5)
check("Click cost cap → bid_price (CPC)", p.get("bid_price") == 0.5 and "conversion_bid_price" not in p, str(p))
p = ag(f_lpv, bid=0.5)
check("Landing page view cost cap → conversion_bid_price (oCPM)", p.get("conversion_bid_price") == 0.5 and "bid_price" not in p, str(p))
check("Landing page view: no pixel, no event", "pixel_id" not in p and "optimization_event" not in p)
p = ag(f_eng, bid=None, pixel="123")
check("Engaged session carries the pixel", p.get("pixel_id") == "123")
check("Engaged session base payload: OCPM, no event (traffic_variants adds it per candidate)",
      p["billing_event"] == "OCPM" and "optimization_event" not in p)
check("website promotion on every traffic goal", p["promotion_type"] == "WEBSITE" and ag(f_click)["promotion_type"] == "WEBSITE")

# ==== 3 · candidates / variants ========================================================
print("\n-- engaged_pairs / traffic_variants --")
SETTINGS.clear()
base = ag(f_eng, pixel="123")
shape = lambda v: (v["optimization_goal"], v.get("optimization_event", ""), "pixel_id" in v)
vs = helpers.traffic_variants(None, f_eng, base)
check("nothing remembered → every candidate, in order", [shape(v) for v in vs] == list(launch.ENGAGED_CANDIDATES), str([shape(v) for v in vs]))
check("every variant bills OCPM", all(v["billing_event"] == "OCPM" for v in vs))
check("a pixel-less shape sends no pixel_id at all (TikTok: \"'pixel_id' is not supported\")",
      all(("pixel_id" in v) == with_px for v, (_g, _e, with_px) in zip(vs, launch.ENGAGED_CANDIDATES)))
check("the pixel never rides without an optimisation event — the shape blue bat_260706145758 refused",
      all(v.get("optimization_event") for v in vs if "pixel_id" in v))
vs_nopx = helpers.traffic_variants(None, f_eng, ag(f_eng, pixel=""))
check("without a pixel only the pixel-less shapes remain", [shape(v) for v in vs_nopx] == [c for c in launch.ENGAGED_CANDIDATES if not c[2]], str([shape(v) for v in vs_nopx]))
SETTINGS[launch.ENGAGED_SETTING_KEY] = json.dumps({"goal": "CONVERT", "event": "ENGAGED_SESSION", "pixel": True})
vs = helpers.traffic_variants(None, f_eng, base)
check("the remembered shape goes first", shape(vs[0]) == ("CONVERT", "ENGAGED_SESSION", True))
check("…and is not repeated", [shape(v) for v in vs].count(("CONVERT", "ENGAGED_SESSION", True)) == 1)
SETTINGS[launch.ENGAGED_SETTING_KEY] = json.dumps({"goal": "ENGAGEMENT_SESSION", "event": ""})
check("an older remembered value without the pixel flag still loads", helpers.remembered_engaged(None) == {"goal": "ENGAGEMENT_SESSION", "event": "", "pixel": True})
SETTINGS[launch.ENGAGED_SETTING_KEY] = "not json"
check("a corrupt setting is ignored, not a crash", helpers.remembered_engaged(None) is None)
SETTINGS.clear()
check("non-engaged fields → the payload untouched", helpers.traffic_variants(None, f_click, {"a": 1}) == [{"a": 1}])
locked = {**f_eng, "_engaged_goal": "ENGAGEMENT_SESSION", "_engaged_goal_locked": True}
vs = helpers.traffic_variants(None, locked, base)
check("a CBO campaign created with one goal locks the ad groups to it",
      vs and all(v["optimization_goal"] == "ENGAGEMENT_SESSION" for v in vs) and len(vs) == 3, str(vs))
check("the value TikTok returned for a live Ads-Manager Engaged-session ad group goes first (ad group 1876342860908641)",
      launch.ENGAGED_CANDIDATES[0][0] == "ENGAGEMENT_SESSION")

print("\n-- campaign payload / candidates --")
f_cbo = launch.synthesize(Template(blob={"traffic_goal": "ENGAGED"}, mode="BUDGET_MODE_DAY", budget=50))
cp = helpers.build_campaign_payload(f_cbo, acct)
check("CBO + Engaged: campaign carries the first candidate goal", cp.get("optimization_goal") == launch.ENGAGED_CANDIDATES[0][0] and cp.get("budget_optimize_on") is True)
cp2 = helpers.build_campaign_payload({**f_cbo, "_engaged_goal": "CONVERT"}, acct)
check("…or the remembered one", cp2["optimization_goal"] == "CONVERT")
cands = helpers.campaign_goal_candidates(f_cbo, cp)
goals = [c["optimization_goal"] for c in cands]
check("campaign candidates = the distinct candidate goals, in order", goals == list(dict.fromkeys(c[0] for c in launch.ENGAGED_CANDIDATES)), str(goals))
check("ABO campaign is never probed", helpers.campaign_goal_candidates(f_eng, helpers.build_campaign_payload(f_eng, acct)) == [helpers.build_campaign_payload(f_eng, acct)])
check("ABO campaign create carries no goal", "optimization_goal" not in helpers.build_campaign_payload(f_eng, acct))
cp_lpv = helpers.build_campaign_payload(launch.synthesize(Template(blob={"traffic_goal": "LPV"}, mode="BUDGET_MODE_DAY", budget=50)), acct)
check("CBO + Landing page view carries TRAFFIC_LANDING_PAGE_PAGE_VIEW".replace("PAGE_PAGE", "PAGE"), cp_lpv["optimization_goal"] == "TRAFFIC_LANDING_PAGE_VIEW")

print("\n-- what moves to the next candidate --")
check("enum complaint walks (engaged)", helpers._walkable(TikTokError(40002, "optimization_goal: invalid value"), engaged=True))
check("generic 'param' complaint walks only while probing", helpers._walkable(TikTokError(40002, "Param error"), engaged=True)
      and not helpers._walkable(TikTokError(40002, "Param error")))
check("a budget complaint never walks", not helpers._walkable(TikTokError(40002, "budget below the minimum"), engaged=True))
check("the real refusal walks: \"'pixel_id' is not supported in /v1.3/adgroup/create/.\"",
      helpers._walkable(TikTokError(40002, "'pixel_id' is not supported in /v1.3/adgroup/create/."), engaged=True))
check("…but not for a non-traffic launch (its variants don't vary the pixel)",
      not helpers._walkable(TikTokError(40002, "'pixel_id' is not supported in /v1.3/adgroup/create/.")))

print("\n-- remember + diagnostics --")
recorded.clear(); SETTINGS.clear()
helpers.remember_traffic_goal(None, f_eng, {"optimization_goal": "ENGAGED_SESSION", "optimization_event": "", "pixel_id": "1"})
check("the accepted shape is stored, pixel flag included", json.loads(SETTINGS[launch.ENGAGED_SETTING_KEY]) == {"goal": "ENGAGED_SESSION", "event": "", "pixel": True})
check("…and reported to Diagnostics", any(c == "engaged-session-accepted" for c, _ in recorded))
helpers.note_engaged_refusal(f_eng, {"optimization_goal": "X"}, TikTokError(40002, "nope"), "ad group")
check("a refusal is reported with the candidate tried", any(c == "engaged-session-refused" and ctx.get("goal") == "X" for c, ctx in recorded))
helpers.remember_traffic_goal(None, f_click, {"optimization_goal": "CLICK"})
check("a Click launch remembers nothing", json.loads(SETTINGS[launch.ENGAGED_SETTING_KEY])["goal"] == "ENGAGED_SESSION")

# ==== 4 · the runner (static) ==========================================================
print("\n-- launch runner --")
run = src[src.index("camp_payload = build_campaign_payload(fields, acct)"):]
check("remembered pair loaded into fields before the campaign payload",
      "_engaged_goal" in src[src.index("camp_payload = build_campaign_payload") - 400: src.index("camp_payload = build_campaign_payload")])
check("campaign create walks the candidate goals", "campaign_goal_candidates(fields, camp_payload)" in run and "_walkable(e, engaged=True)" in run)
check("a CBO campaign locks the goal for its ad groups", '"_engaged_goal_locked": True' in run)
check("ad-group variants come from traffic_variants", "variants: list[dict] = traffic_variants(db, fields, base_payload)" in run)
check("the accepted ad-group payload is remembered on the first ad group",
      re.search(r"if is_engaged\(fields\) and i == 0:\s*\n\s*remember_traffic_goal\(db, fields, ag_payload\)", run) is not None)
check("the launch result records the accepted goal", "log.optimization_event = (str(ag_payload.get(\"optimization_goal\"" in run)
check("Engaged session resolves the pixel like a conversion launch", "or is_engaged(fields))" in src and "not is_engaged(fields) and not fields.get(\"optimization_event\")" in src)

# ==== 5 · form + JS + parity + list ====================================================
print("\n-- form, JS, parity, list --")
form = open(os.path.join(ROOT, "app", "templates", "template_form.html"), encoding="utf-8").read()
check("goal cards in the form", 'id="trafficGoalCards"' in form and 'name="traffic_goal"' in form)
check("pixel picker for Engaged session", 'id="trafficPixel"' in form and 'name="traffic_pixel"' in form)
check("pixel option value resolves per account (code first)", 'value="{{ px.pixel_code or px.pixel_id }}"' in form)
js = open(os.path.join(ROOT, "app", "static", "preset-builder.js"), encoding="utf-8").read()
check("goal field shown only for Traffic", 'tgField.hidden = objSel.value !== "TRAFFIC"' in js)
check("pixel picker shown only for Engaged session", 'tgPixel.hidden = tg.sel.value !== "ENGAGED"' in js)
check("picking a goal repaints the pixel picker", 'bindCards("trafficGoalCards", "trafficGoal", syncTrafficPixel)' in js)
tr = open(os.path.join(ROOT, "app", "routes", "templates_routes.py"), encoding="utf-8").read()
check("the blob stores goal + pixel", '"traffic_goal": val("traffic_goal", "CLICK")' in tr and '"traffic_pixel": val("traffic_pixel")' in tr)
check("an unknown goal value falls back to Click", 'in TRAFFIC_GOALS else "CLICK"' in tr)
par = open(os.path.join(ROOT, "app", "parity.py"), encoding="utf-8").read()
check("parity watch knows the candidates (no false 'not offered' on our own ad groups)", "launch.ENGAGED_CANDIDATES" in par)
lst = open(os.path.join(ROOT, "app", "templates", "templates_list.html"), encoding="utf-8").read()
check("presets list shows the goal", "'Landing page view' if blob.get('traffic_goal') == 'LPV' else 'Engaged session'" in lst)
cfg = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
check("static version bumped", 'STATIC_VERSION = "111"' in cfg)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
