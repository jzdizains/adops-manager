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
_mod("app.models", Template=Template, AdAccount=AdAccount, SparkCode=type("SparkCode", (), {}), Creative=type("Creative", (), {}), AdText=type("AdText", (), {}))
_mod("sqlalchemy.orm", Session=object)

import importlib
launch = importlib.import_module("app.routes.launch")
em = importlib.import_module("app.error_messages")

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
end = src.index("def _launch_smart_plus(")
helpers = types.ModuleType("app.routes.campaigns")
helpers.__dict__.update({"models": sys.modules["app.models"], "tiktok_api": tiktok_api, "queries": queries,
                         "launch_mod": launch, "Session": object, "datetime": datetime, "acct_time": importlib.import_module("app.acct_time"), "timezone": timezone, "re": re, "json": json,
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
# v120: the preset's Display Card add-on reaches the launch fields (it was saved but never carried → never placed)
f_card = launch.synthesize(Template(blob={"display_card_id": 7}))
check("a preset's display card is carried into the launch fields", f_card.get("display_card_id") == 7)
check("no card → None (engine treats it as off)", f_none.get("display_card_id") is None)

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
check("no shape carries an optimisation event (neither spelling is in TikTok's enum) and the pixel shape goes first",
      all(not v.get("optimization_event") for v in vs) and "pixel_id" in vs[0])
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
      vs and all(v["optimization_goal"] == "ENGAGEMENT_SESSION" for v in vs) and len(vs) == 2, str(vs))
check("the value TikTok returned for a live Ads-Manager Engaged-session ad group goes first (ad group 1876342860908641)",
      launch.ENGAGED_CANDIDATES[0][0] == "ENGAGEMENT_SESSION")

print("\n-- campaign payload / candidates --")
f_cbo = launch.synthesize(Template(blob={"traffic_goal": "ENGAGED"}, mode="BUDGET_MODE_DAY", budget=50))
cp = helpers.build_campaign_payload(f_cbo, acct)
check("CBO + Engaged: campaign carries the first candidate goal", cp.get("optimization_goal") == launch.ENGAGED_CANDIDATES[0][0] and cp.get("budget_optimize_on") is True)
cp2 = helpers.build_campaign_payload({**f_cbo, "_engaged_goal": "CONVERT"}, acct)
check("…or the remembered one", cp2["optimization_goal"] == "CONVERT")
cands = helpers.campaign_goal_candidates(f_cbo, cp)
check("CBO campaign candidates carry each candidate goal, in order", [c["optimization_goal"] for c in cands] == list(dict.fromkeys(c[0] for c in launch.ENGAGED_CANDIDATES)), str(cands))
check("no campaign_automation_type is ever sent (TikTok ignores it on create — campaign 1876347219692161 came back MANUAL)",
      "campaign_automation_type" not in src[src.index("def build_campaign_payload"):src.index("def build_ad_payload")])
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
check("Engaged session implies Smart+ before any validation (spark required, carousel refused, Smart+ chain used)",
      'if is_engaged(fields) and not fields.get("smart_plus"):' in src and '"smart_plus": True, "_smart_plus_implied": True' in src
      and src.index('"_smart_plus_implied": True') < src.index('use_library = fields.get("creative_source") == "library"'))
check("…and the launch result names it", '"ENGAGEMENT_SESSION · Smart+"' in src)
spc_c = src[src.index("def build_spc_campaign_payload"):src.index("def build_spc_adgroup_payload")]
check("Smart+ ABO campaign sends no budget_mode (INFINITE refused: 'Budget mode is invalid', blue bat_260706150309)",
      'payload["budget_mode"] = "BUDGET_MODE_INFINITE"' not in spc_c)
f_abo = {"objective_type": "TRAFFIC", "campaign_budget_mode": "ABO", "adgroup_budget": 25.0, "template_name": "T", "campaign_name_pattern": "x"}
cpv = helpers.spc_campaign_variants(f_abo, {"campaign_name": "c", "objective_type": "TRAFFIC"})
check("Smart+ ABO: budget-less first, explicit ABO (budget_optimize_on false) second, CBO with the endpoint's DYNAMIC daily mode last — never BUDGET_MODE_DAY (17 Sep: invalid with and without CBO)",
      [(("budget_mode" in c), c.get("budget_optimize_on"), on) for c, on in cpv] == [(False, None, False), (False, False, False), (True, True, True)]
      and cpv[2][0]["budget"] == 25.0 and cpv[2][0]["budget_mode"] == "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
      and not any(c.get("budget_mode") == "BUDGET_MODE_DAY" for c, _ in cpv), str(cpv))
check("Smart+ CBO preset: one shape, the preset's daily mode translated to the Smart+ enum",
      helpers.spc_campaign_variants(f_abo, {"budget_optimize_on": True, "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET", "budget": 50.0}) == [({"budget_optimize_on": True, "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET", "budget": 50.0}, True)]
      and 'payload["budget_mode"] = SPC_BUDGET_MODE.get(mode, mode)' in spc_c
      and helpers.SPC_BUDGET_MODE == {"BUDGET_MODE_DAY": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET", "BUDGET_MODE_TOTAL": "BUDGET_MODE_TOTAL"})
check("only a budget complaint moves to the campaign-budget shape", 'and "budget" in (e.message or "").lower():' in src[src.index("def _launch_smart_plus"):])
check("a failed Smart+ launch deletes the empty shell through the Smart+ endpoint",
      'tiktok_api.smart_plus_campaign_status_update(acct.access_token, acct.advertiser_id, [shell], "DELETE")' in src and "ad_created = True                 # the chain only returns once the ad exists" in src)
spc_ag = src[src.index("def build_spc_adgroup_payload"):src.index("def build_spc_ad_payload")]
check("Smart+ ad group for Engaged session: ENGAGEMENT_SESSION on oCPM with the pixel and no event (the stored shape)",
      'goal = "ENGAGEMENT_SESSION"' in spc_ag and '"billing_event": "CPC" if goal == "CLICK" else "OCPM"' in spc_ag and 'elif engaged and pixel_id:' in spc_ag)
tk = open(os.path.join(ROOT, "app", "tiktok_api.py"), encoding="utf-8").read()
check("Smart+ request_id is an int64 string, not a uuid (blue bat_260706150312: 'strconv.ParseInt … invalid syntax')",
      "random.getrandbits(62) | 1" in tk and "uuid.uuid4().hex" not in tk[tk.index("def _request_id"):tk.index("def smart_plus_campaign_create")])
import random as _r
_ns = {}; exec("import random\n" + tk[tk.index("def _request_id"):tk.index("def smart_plus_campaign_create")], _ns)
rid = _ns["_request_id"]()
check("…parses as a positive int64", rid.isdigit() and 0 < int(rid) < 2**63 and rid != _ns["_request_id"]())
spc = src[src.index("def build_spc_adgroup_payload"):src.index("def build_spc_ad_payload")]
check("legacy Smart+ ad group honours Landing page view and puts the cap on the right bid field",
      'goal = "TRAFFIC_LANDING_PAGE_VIEW"' in spc and 'if payload["billing_event"] == "OCPM":' in spc)
check("ad-group variants come from traffic_variants", "variants: list[dict] = traffic_variants(db, fields, base_payload)" in run)
check("the accepted ad-group payload is remembered on the first ad group",
      re.search(r"if is_engaged\(fields\) and i == 0:\s*\n\s*remember_traffic_goal\(db, fields, ag_payload\)", run) is not None)
check("the launch result records the accepted goal", "log.optimization_event = (str(ag_payload.get(\"optimization_goal\"" in run)
check("Auto CTA asks the recommender for the nearest known traffic goal (its enum lacks Engaged session)",
      'goal = "TRAFFIC_LANDING_PAGE_VIEW"' in src[src.index("def resolve_cta_portfolio"):src.index("def url_safe_name")]
      and "optimization_goal=goal)" in src[src.index("def resolve_cta_portfolio"):src.index("def url_safe_name")])
f_auto = {"template_name": "T", "call_to_action": "AUTO"}
_sp = {"identity_id": "i", "identity_type": "TT_USER", "item_id": "v"}
helpers.spark_ad_format = lambda ref, spark: "SINGLE_VIDEO"
_ad = helpers.build_spc_ad_payload(f_auto, "ag1", _sp, None)
check("Smart+ ad with Auto CTA sends at most 3 buttons (18 Sep: 'call_to_action_list: maximum number of items is 3')",
      len(_ad["call_to_action_list"]) == 3 and _ad["call_to_action_list"][0] == {"call_to_action": "LEARN_MORE"} and helpers.SPC_CTA_MAX == 3, str(_ad["call_to_action_list"]))
_bc = helpers.build_spc_ad_payload(f_auto, "ag1", {**_sp, "identity_type": "BC_AUTH_TT", "identity_authorized_bc_id": "bc9"}, None)
check("Smart+ ad creative carries the identity's BC id for BC_AUTH_TT (18 Sep: 'identity_bc_id is required')",
      _bc["creative_list"][0]["creative_info"].get("identity_authorized_bc_id") == "bc9"
      and "identity_authorized_bc_id" not in _ad["creative_list"][0]["creative_info"], str(_bc["creative_list"]))
check("…a chosen CTA is sent alone", helpers.build_spc_ad_payload({**f_auto, "call_to_action": "SHOP_NOW"}, "ag1", _sp, None)["call_to_action_list"] == [{"call_to_action": "SHOP_NOW"}])
# the Smart+ chain itself, against a fake TikTok that refuses "advanced creatives"
_ls = src[src.index("def spc_ad_shapes("):src.index("\ndef ", src.index("def _launch_smart_plus(") + 10)]
_ns2 = dict(helpers.__dict__)
class _TT:
    calls = []
    refuse = "addon"          # addon: only the add-on is "advanced" | multi: add-on and several buttons | all: everything
    class TikTokError(Exception):
        def __init__(self, code, message): super().__init__(message); self.code = code; self.message = message
    @staticmethod
    def smart_plus_campaign_create(tok, adv, p): _TT.calls.append(("camp", p)); return {"campaign_id": "c1"}
    @staticmethod
    def smart_plus_adgroup_create(tok, adv, p): _TT.calls.append(("ag", p)); return {"adgroup_id": "g1"}
    @staticmethod
    def smart_plus_ad_create(tok, adv, p):
        _TT.calls.append(("ad", p))
        advanced = bool(p.get("interactive_add_on_list")) or (_TT.refuse in ("multi", "all") and len(p["call_to_action_list"]) > 1) or _TT.refuse == "all"
        if advanced:
            raise _TT.TikTokError("40002", "The selected advanced creative is not supported. Please select another one.")
        return {"ad_id": "a1"}
_ns2.update({"tiktok_api": _TT, "ConfigError": Exception, "is_engaged": lambda f: False,
             "build_spc_campaign_payload": lambda f, a: {"campaign_name": "c", "objective_type": "TRAFFIC"}})
exec(compile(_ls, "launch-smart-plus", "exec"), _ns2)
_acct = types.SimpleNamespace(access_token="t", advertiser_id="1", owner_bc_id="bc1")
_f = {"destination_type": "website", "objective_type": "TRAFFIC", "traffic_goal": "CLICK", "template_name": "T", "schedule_type": "SCHEDULE_FROM_NOW",
      "campaign_budget_mode": "ABO", "adgroup_budget": 25.0, "call_to_action": "AUTO", "_display_card_portfolio_id": "card9", "location_ids": ["6252001"]}
def _run():
    _TT.calls.clear(); log = types.SimpleNamespace(campaign_id="", optimization_event="")
    cid, _ = _ns2["_launch_smart_plus"](_acct, _f, {**_sp, "identity_type": "BC_AUTH_TT"}, None, "", log=log)
    return cid, [p for k, p in _TT.calls if k == "ad"], log
_cid, _ads, _log = _run()
check("Smart+ ad refused for its add-on ('advanced creative is not supported') lands on the retry without it, and the result says so",
      _cid == "c1" and len(_ads) == 2 and "interactive_add_on_list" in _ads[0] and "interactive_add_on_list" not in _ads[1]
      and _log.optimization_event == "CLICK · Smart+ · no display card (TikTok refuses add-ons on this Smart+ ad)", str((_ads, _log.optimization_event)))
check("…a BC identity without a stored BC id gets the account's Business Center on ad group and creative",
      [p for k, p in _TT.calls if k == "ag"][0].get("identity_authorized_bc_id") == "bc1" and _ads[1]["creative_list"][0]["creative_info"].get("identity_authorized_bc_id") == "bc1")
_TT.refuse = "multi"; _cid, _ads, _log = _run()
check("…still refused → one button; both notes on the result", _cid == "c1" and len(_ads) == 3 and _ads[2]["call_to_action_list"] == [{"call_to_action": "LEARN_MORE"}]
      and _log.optimization_event.endswith("no display card (TikTok refuses add-ons on this Smart+ ad) · one button, LEARN_MORE (TikTok refuses several on this Smart+ ad)"), _log.optimization_event)
_TT.refuse = "all"
try:
    _run(); _err = ""
except Exception as e:  # noqa: BLE001
    _err = str(e)
check("…refused in every shape → the error names the post as the cause, nothing is swallowed",
      "refused even with one button and no add-on" in _err and "video spark" in _err, _err)
_TT.refuse = "addon"
check("the operator's message for that refusal is honest and actionable",
      "won't run this creative on a Smart+ ad" in em.explain("40002", "The selected advanced creative is not supported. Please select another one.")["friendly"])
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
check("creative-type filter removed (all-carousel made it dead weight); search box stays",
      'id="prCreative"' not in lst and 'data-c="spark"' not in lst and '#prCreative button' not in lst
      and "r.dataset.creative === c" not in lst and 'id="prQ"' in lst and "r.dataset.search.indexOf(q)" in lst)
# v155.5: Instant Form lead gen — TikTok refuses custom attribution windows there (live 23 Sep:
# 40002 "Attribution window combination is invalid in this ad scenario" on 1-day click + 1-day view)
print("\n-- attribution windows by scenario --")
base = dict(f_conv, click_attribution_window="ONE_DAY", view_attribution_window="ONE_DAY")
p_form = ag(dict(base, objective_type="LEAD_GENERATION", destination_type="lead_form", lead_form_id="7688798794846077205",
                 optimization_goal="LEAD_GENERATION"), pixel="")
check("Instant Form: no click/view window is sent (TikTok's own default applies)",
      p_form.get("promotion_target_type") == "INSTANT_PAGE" and "click_attribution_window" not in p_form and "view_attribution_window" not in p_form, str(p_form))
p_web = ag(dict(base, objective_type="LEAD_GENERATION", destination_type="pixel"), pixel="123")
check("website lead gen keeps the preset's windows (TikTok accepts 1/1 and 7/7 there — seen on live ad groups)",
      p_web.get("promotion_target_type") == "EXTERNAL_WEBSITE" and p_web.get("click_attribution_window") == "ONE_DAY" and p_web.get("view_attribution_window") == "ONE_DAY", str(p_web))
# v155.7: "Lead Generation agreement has not been signed yet" on an Instant Form ad → one retry in the
# reference tool's shape (it launches Instant Form ads with ONE fixed button and no ad text)
print("\n-- Instant Form ad: the plain-shape retry --")
live = {"adgroup_id": "1877149100479490", "creatives": [{"ad_name": "x ad", "ad_format": "SINGLE_VIDEO", "ad_text": "Have you checked it?",
        "call_to_action_id": "7688802756777364501", "identity_id": "i", "identity_type": "BC_AUTH_TT", "tiktok_item_id": "7688392808218086669",
        "page_id": "7688798794846077205"}]}                    # exactly what TikTok refused on 23 Sep
lf_fields = {"destination_type": "lead_form", "call_to_action": "AUTO"}
pl = helpers.lead_form_plain(live, lf_fields)
c = pl["creatives"][0]
check("Dynamic CTA → one fixed button (Learn more), no ad text on the Spark post, form + post kept",
      "call_to_action_id" not in c and c["call_to_action"] == "LEARN_MORE" and "ad_text" not in c
      and c["page_id"] == "7688798794846077205" and c["tiktok_item_id"] == "7688392808218086669", str(c))
check("a fixed button in the preset is kept", helpers.lead_form_plain(live, {"destination_type": "lead_form", "call_to_action": "SIGN_UP"})["creatives"][0]["call_to_action"] == "SIGN_UP")
check("the original payload isn't changed", "call_to_action_id" in live["creatives"][0] and "ad_text" in live["creatives"][0])
check("nothing to change → no retry", helpers.lead_form_plain({"creatives": [{"call_to_action": "LEARN_MORE", "page_id": "1"}]}, lf_fields) is None)
check("only for Instant Form ads", helpers.lead_form_plain(live, {"destination_type": "website", "call_to_action": "AUTO"}) is None)
lib_ad = {"creatives": [{"ad_text": "Hi", "call_to_action_id": "9", "video_id": "v", "page_id": "1"}]}
check("an uploaded video keeps its ad text (only a Spark post has a caption of its own)",
      helpers.lead_form_plain(lib_ad, lf_fields)["creatives"][0].get("ad_text") == "Hi")
check("matched on TikTok's exact words (with its 'has not be signed' typo)",
      helpers.is_lead_agreement("Lead Generation agreement has not be signed yet.") and not helpers.is_lead_agreement("Invalid landing page"))
csrc = open(os.path.join(ROOT, "app", "routes", "campaigns.py"), encoding="utf-8").read()
check("the launch retries ONCE on that error and writes the outcome to Diagnostics either way",
      "plain = lead_form_plain(ad_payload, fields) if is_lead_agreement(e.message) else None" in csrc
      and '"lead-form-plain-ok"' in csrc and '"lead-form-plain-refused"' in csrc and "raise e2" in csrc)
# v155.8: Instant Form launches go out as Smart+, shaped like the ad group TikTok accepted when
# built by hand in Ads Manager (blue bat_260706030017, ad group 1877159492816945, 23 Sep)
print("\n-- Instant Form as Smart+ --")
spark_ref = {"identity_id": "51cc6c65", "identity_type": "BC_AUTH_TT", "identity_authorized_bc_id": "7658285881817202708",
             "item_id": "7688392808218086669", "item_type": "VIDEO"}
lf = dict(f_conv, objective_type="LEAD_GENERATION", destination_type="lead_form", lead_form_id="7688798794846077205",
          lead_form_name="Untitled form 9/22/26, 17:16", template_name="Lead preset", creative_source="spark",
          click_attribution_window="ONE_DAY", view_attribution_window="ONE_DAY", age_groups=["AGE_13_17", "AGE_25_34"],
          call_to_action="AUTO", adgroup_budget=30, adgroup_budget_mode="BUDGET_MODE_DAY", schedule_type="SCHEDULE_FROM_NOW",
          location_ids=["6252001"], gender="GENDER_UNLIMITED", campaign_budget_mode="ABO", ad_text="Have you checked it?")
check("a Spark Instant Form launch is sent as Smart+", helpers.lead_form_as_smart_plus(lf))
check("…a library video or carousel stays regular (not supported on the Smart+ path)",
      not helpers.lead_form_as_smart_plus(dict(lf, creative_source="library")) and not helpers.lead_form_as_smart_plus(dict(lf, creative_source="carousel"))
      and not helpers.lead_form_as_smart_plus(dict(lf, destination_type="pixel")))
g = helpers.build_spc_adgroup_payload(lf, "c1", spark_ref, "", None)
working = {"promotion_type": "LEAD_GENERATION", "promotion_target_type": "INSTANT_PAGE", "optimization_goal": "LEADS",
           "optimization_event": "FORM", "billing_event": "OCPM", "bid_type": "BID_TYPE_NO_BID"}
check("ad group = the one TikTok accepted (LEAD_GENERATION · INSTANT_PAGE · LEADS · FORM · OCPM · no bid)",
      all(g.get(k) == v for k, v in working.items()), str({k: g.get(k) for k in working}))
check("no pixel and no attribution window sent (TikTok attached its own; its default is what the working one carries)",
      "pixel_id" not in g and "click_attribution_window" not in g and "view_attribution_window" not in g)
check("Smart+'s own daily budget type (BUDGET_MODE_DAY was 'Invalid budget type', 24 Sep) — as on the working ad group",
      g["budget_mode"] == "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
      and helpers.build_spc_adgroup_payload(dict(lf, adgroup_budget_mode="BUDGET_MODE_TOTAL"), "c1", spark_ref, "", None)["budget_mode"] == "BUDGET_MODE_TOTAL")
check("adults only, the form's budget and the post's identity on the ad group",
      g["targeting_spec"]["age_groups"] == ["AGE_25_34"] and g["budget"] == 30.0 and g["identity_type"] == "BC_AUTH_TT"
      and g["identity_authorized_bc_id"] == "7658285881817202708" and g["schedule_type"] == "SCHEDULE_FROM_NOW")
g2 = helpers.build_spc_adgroup_payload(lf, "c1", spark_ref, "", 4.5)
check("a cost cap goes on conversion_bid_price (oCPM)", g2["bid_type"] == "BID_TYPE_CUSTOM" and g2["conversion_bid_price"] == 4.5 and "bid_price" not in g2)
a = helpers.build_spc_ad_payload(lf, "ag1", spark_ref, None)
check("the form is the ad's destination (page_list), never alongside a URL",
      a.get("page_list") == [{"page_id": "7688798794846077205"}] and "landing_page_url_list" not in a)
check("'Auto' button = lead buttons, no 'Shop now'", [c["call_to_action"] for c in a["call_to_action_list"]] == ["SIGN_UP", "LEARN_MORE", "APPLY_NOW"])
check("a website Smart+ ad is unchanged (URL, general buttons)",
      "page_list" not in helpers.build_spc_ad_payload(dict(lf, destination_type="website", landing_page_url="https://x.test"), "ag", spark_ref, None)
      and helpers.build_spc_ad_payload(dict(lf, destination_type="website", landing_page_url="https://x.test"), "ag", spark_ref, None)["landing_page_url_list"])
csrc2 = open(os.path.join(ROOT, "app", "routes", "campaigns.py"), encoding="utf-8").read()
check("the preset form points at the Lead Generation Terms step", "needs TikTok's Lead Generation Terms accepted once" in open(os.path.join(ROOT, "app", "templates", "template_form.html"), encoding="utf-8").read())
check("v155.13: the preset's own Smart+ switch decides again (no forced Smart+), and Smart+ still accepts the Instant Form destination",
      "if lead_form_as_smart_plus(fields):" not in csrc2 and csrc2.count('not in ("pixel", "website", "lead_form")') == 2)
tf = open(os.path.join(ROOT, "app", "templates", "template_form.html"), encoding="utf-8").read()
check("the preset form says so under the attribution fields", "Instant Form lead gen always uses TikTok’s default" in tf)
cfg = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
check("static version bumped", 'STATIC_VERSION = "183"' in cfg)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
