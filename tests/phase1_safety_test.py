"""Phase 1 safety fixes + warm-up (v145) — from the Albith comparison.

  1. OAuth callback verifies `state` (login-CSRF: someone else's TikTok login attached to
     your workspace) and needs a signed-in user.
  2. Test mode can't be switched on a deployed host (ADOPS_DISABLE_BG used to switch off 2FA).
  3. TikTok retries are idempotency-aware: a CREATE is never re-sent after the request may
     have landed (duplicate campaigns); pause / budget / report calls ARE retried.
     Failed accounts that still have live ads — or may have an unseen campaign — are never
     relaunched by Retry failed or the queue.
  4. Per-user scope on admin + bulk actions (cookies, diagnostics, jobs, alerts, resume-all…).
  5. Launch payloads: lead-gen Instant Form shape, no 13-17 on lead gen, Reach frequency cap,
     smooth pacing with No-Bid, display card on uploaded-video ads.
  6. Warm-up: recipe, own-country targeting, auto-pause decision, page + poller wiring.

The real tiktok_api runs against an httpx MockTransport (real timeouts, real call counts);
the payload builders and warm-up helpers run on light stubs; routes are source-asserted
(they need the full app)."""
import json, os, subprocess, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
FUT = "from __future__ import annotations\n"
def grab(src, name):
    i = src.index("def " + name + "(")
    ends = [e for e in (src.find(m, i + 1) for m in ("\ndef ", "\n@", "\nclass ", "\nMAYBE_", "\nADULT_")) if e != -1]
    return src[i:(min(ends) if ends else len(src))]

# ---------------------------------------------------------------------------------------
print("-- 1. OAuth callback: state must match, single use --")
oa = read("app/routes/oauth.py")
ns = {"secrets": __import__("secrets")}
exec(FUT + grab(oa, "_state_ok"), ns)
class Req:
    def __init__(self, session, q):
        self.session, self.query_params = session, q
check("matching state passes", ns["_state_ok"](Req({"oauth_state": "abc"}, {"state": "abc"})))
r = Req({"oauth_state": "abc"}, {"state": "abc"}); ns["_state_ok"](r)
check("…and is consumed (a replayed link fails)", not ns["_state_ok"](Req(r.session, {"state": "abc"})))
check("a link with someone else's code and no state is refused", not ns["_state_ok"](Req({"oauth_state": "abc"}, {})))
check("a state from another browser is refused", not ns["_state_ok"](Req({}, {"state": "abc"})))
check("a wrong state is refused", not ns["_state_ok"](Req({"oauth_state": "abc"}, {"state": "abd"})))
check("the callback checks state BEFORE exchanging the code, and needs a signed-in user",
      oa.index("if not _state_ok(request)") < oa.index("exchange_auth_code(auth_code)")
      and "You're signed out" in oa)

# ---------------------------------------------------------------------------------------
print("-- 2. test mode never on a deployed host --")
def cfg(env):
    code = "import sys; sys.path.insert(0, %r); import importlib.util as u; s=u.spec_from_file_location('c', %r); m=u.module_from_spec(s); s.loader.exec_module(m); print(m.TEST_MODE, m.REQUIRE_2FA)" % (ROOT, os.path.join(ROOT, "app", "config.py"))
    e = {k: v for k, v in os.environ.items() if k not in ("RENDER", "ADOPS_DEPLOYED", "ADOPS_TEST", "ADOPS_DISABLE_BG")}
    e.update(env)
    return subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True).stdout.split()
check("local tests keep test mode (ADOPS_DISABLE_BG=1)", cfg({"ADOPS_DISABLE_BG": "1"}) == ["True", "False"])
check("on Render, ADOPS_DISABLE_BG=1 no longer switches off 2FA", cfg({"ADOPS_DISABLE_BG": "1", "RENDER": "true"}) == ["False", "True"])
check("even ADOPS_TEST=1 is refused on a deployed host", cfg({"ADOPS_TEST": "1", "ADOPS_DEPLOYED": "1"}) == ["False", "True"])
check("plain production: no test mode, 2FA required", cfg({}) == ["False", "True"])

# ---------------------------------------------------------------------------------------
print("-- 3. retries: creates never duplicated, pauses never lost --")
import httpx, time as _t
pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]; sys.modules["app"] = pkg
import importlib
api = importlib.import_module("app.tiktok_api")
_t.sleep = lambda s: None                     # backoff instantly in tests
calls = []
def use(handler):
    calls.clear()
    def h(req):
        calls.append(req.url.path)
        return handler(req, len(calls))
    api._shared_client = httpx.Client(transport=httpx.MockTransport(h))
ok = lambda data=None: httpx.Response(200, json={"code": 0, "message": "OK", "data": data or {}})

use(lambda req, n: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)))
try:
    api.create_campaign("t", "1", {"campaign_name": "x"}); got = None
except api.TikTokError as e:
    got = e
check("a create whose response timed out is sent ONCE (no duplicate campaign)", len(calls) == 1, calls)
check("…and the error says it may exist — check before relaunching",
      got is not None and got.maybe_sent and "may or may not have been created" in got.message)

use(lambda req, n: (_ for _ in ()).throw(httpx.ConnectError("refused", request=req)) if n < 3 else ok({"campaign_id": "C1"}))
res = api.create_adgroup("t", "1", {"adgroup_name": "x"})
check("a create that never reached TikTok (connect failed) IS retried", len(calls) == 3 and res.get("campaign_id") == "C1", calls)

use(lambda req, n: httpx.Response(200, json={"code": 40100, "message": "rate limited"}) if n < 2 else ok())
api.update_campaign_status("t", "1", ["C1"], "DISABLE")
check("a rule's pause survives one rate-limit hiccup (retried)", len(calls) == 2, calls)
use(lambda req, n: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)) if n < 2 else ok())
api.update_campaign_status("t", "1", ["C1"], "DISABLE")
check("…and a read timeout on a pause is retried too (idempotent)", len(calls) == 2, calls)
use(lambda req, n: httpx.Response(200, json={"code": 50000, "message": "internal"}) if n < 2 else ok({"list": [{"x": 1}]}))
rows = api.get_report("t", "1", dimensions=["campaign_id"], metrics=["spend"], start_date="2026-09-23", end_date="2026-09-23")
check("the spend report behind the rules is retried", len(calls) == 2 and rows == [{"x": 1}], calls)
use(lambda req, n: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)))
try:
    api.api_get_retry("/advertiser/info/", "t", {})
except api.TikTokError:
    pass
check("a GET read timeout is not retried 3× (would stall a whole sweep)", len(calls) == 1, calls)
api._shared_client = None

cp = read("app/routes/campaigns.py")
ns = {}
exec(FUT + "MAYBE_CREATED_MARK = 'may or may not have been created'\n" + grab(cp, "relaunch_safe"), ns)
L = types.SimpleNamespace
safe = ns["relaunch_safe"]
check("a clean failure (nothing created) may be relaunched", safe(L(ok=False, campaign_id="", error_message="bad", error_technical="")))
check("a failure that left LIVE ads is never relaunched (would be a 2nd campaign)", not safe(L(ok=False, campaign_id="C9", error_message="", error_technical="")))
check("a create that may have landed unseen is never relaunched",
      not safe(L(ok=False, campaign_id="", error_message="", error_technical="code=HTTP message=… may or may not have been created …")))
check("Retry failed and the queue both use it",
      "if relaunch_safe(l):\n            fresh.append(l.advertiser_id)" in cp      # (v146: retry_plan — unsafe ones resume instead)
      and "if not engine.relaunch_safe(log):" in read("app/queue_worker.py"))
check("a partly-live failure says so in plain words", "Part of this launch went live" in cp)
check("a network failure never walks to another ad-group variant", 'if str(e.code) == "HTTP":' in grab(cp, "_walkable"))

# ---------------------------------------------------------------------------------------
print("-- 4. per-user scope on admin + bulk actions --")
jp = read("app/routes/jobs_page.py")
check("jobs carry an owner (defaults to the request's workspace)",
      "owner_user_id = Column(Integer, nullable=True, index=True, default=ctx.owner_default)" in read("app/models.py").split("class Job(")[1][:2500])
check("'job finished' toasts go only to the person whose job it was", "_notify_owner_ids(request, db))" in jp)
check("running list / history / counts follow the workspace in view", jp.count("_view_owner_ids(request, db)") >= 5)
check("cancel refuses a job outside your workspace", "That job isn't in your workspace." in jp)
check("clear-finished / cancel-queued only touch your own jobs",
      "jobs.clear_finished(db, _view_owner_ids(request, db))" in jp and "jobs.cancel_queued(db, _view_owner_ids(request, db))" in jp)
ck = read("app/routes/cookies_admin.py"); dg = read("app/routes/diagnostics.py")
check("the company-wide TikTok cookie is owner-only (page, save, extension push)", ck.count("guard.is_owner(request)") == 3)
check("the cross-workspace error feed is owner-only (page, json, seen)", dg.count("guard.is_owner(request)") == 4)   # + the test-mode switch (v153)
check("Cookies is hidden from buyers' tabs and ⌘K", '"/cookies"' in read("app/nav.py").split("OWNER_ONLY_TABS")[1][:80]
      and '"/cookies"' in read("app/templating.py").split("OWNER_ONLY_JUMP")[1][:160])
ib = read("app/inbox.py"); al = read("app/routes/alerts.py"); ir = read("app/routes/inbox.py")
check("one visibility rule shared by the inbox list and the dismiss actions", "def alert_visible(a" in ib and "if not alert_visible(a, scope):" in ib)
check("ack-all / dismiss-all clear only what THIS person sees",
      "inbox_mod.visible_unacked(db, scope_mod.for_request(request, db))" in al and "inbox_mod.visible_unacked(db, sc)" in ir)
au = read("app/routes/automation.py")
check("Resume all only resumes the workspace's own campaigns", "if sc.allows(r.advertiser_id)}" in au)
check("single resume refuses a campaign outside the workspace", "isn't+in+your+workspace" in au)
check("Verify pending only walks the workspace's launches", "sc.allows(l.advertiser_id)]" in read("app/routes/status.py"))

# ---------------------------------------------------------------------------------------
print("-- 5. launch payloads --")
from datetime import datetime, timezone
lm = types.ModuleType("app.models"); lm.AdAccount = object; lm.SparkCode = object
ns = {"datetime": datetime, "timezone": timezone, "models": lm, "acct_time": importlib.import_module("app.acct_time"),
      "_cta": lambda f: {"call_to_action": f.get("call_to_action") or "LEARN_MORE"},
      "spark_ad_format": lambda a, b: "SINGLE_VIDEO"}
src = "ADULT_AGE_GROUPS = [\"AGE_18_24\", \"AGE_25_34\", \"AGE_35_44\", \"AGE_45_54\", \"AGE_55_100\"]\n"
for fn in ("lead_gen_ages", "build_adgroup_payload", "apply_destination", "build_ad_payload", "build_library_ad_payload"):
    src += grab(cp, fn) + "\n"
exec(FUT + src, ns)
base = {"template_name": "T", "duplicates": 1, "cost_cap_ladder": [], "location_ids": ["6252001"], "gender": "GENDER_UNLIMITED",
        "billing_event": "OCPM", "schedule_type": "SCHEDULE_FROM_NOW", "adgroup_budget": 20, "bid_type": "BID_TYPE_NO_BID"}
def ag(**kw):
    return ns["build_adgroup_payload"]({**base, **kw}, None, "C1", 0, None, "")
lf = ag(objective_type="LEAD_GENERATION", destination_type="lead_form", lead_form_id="7681606999372579090",
        optimization_goal="LEAD_GENERATION", age_groups=["AGE_13_17", "AGE_18_24"])
check("Instant Form ad group: LEAD_GENERATION → INSTANT_PAGE with the form on the ad group",
      lf.get("promotion_type") == "LEAD_GENERATION" and lf.get("promotion_target_type") == "INSTANT_PAGE"
      and lf.get("page_id") == "7681606999372579090", lf)
check("lead gen never targets 13-17", lf["age_groups"] == ["AGE_18_24"])
check("lead gen with no ages picked = every ADULT bracket (not all ages)",
      ag(objective_type="LEAD_GENERATION", destination_type="lead_form", optimization_goal="LEAD_GENERATION")["age_groups"]
      == ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"])
check("other objectives keep 13-17 when the preset has it",
      ag(objective_type="TRAFFIC", destination_type="website", optimization_goal="CLICK", age_groups=["AGE_13_17"])["age_groups"] == ["AGE_13_17"])
check("accelerated pacing is forced smooth under No-Bid",
      ag(objective_type="TRAFFIC", destination_type="website", optimization_goal="CLICK", pacing="PACING_MODE_FAST")["pacing"] == "PACING_MODE_SMOOTH")
check("…but kept with a cost cap (bid given)",
      ns["build_adgroup_payload"]({**base, "objective_type": "TRAFFIC", "destination_type": "website", "optimization_goal": "CLICK",
                                   "billing_event": "CPC", "pacing": "PACING_MODE_FAST"}, None, "C1", 0, 0.4, "")["pacing"] == "PACING_MODE_FAST")
rc = ag(objective_type="REACH", destination_type="none", optimization_goal="REACH", billing_event="CPM")
check("Reach carries a frequency cap (3 per 7 days)", rc.get("frequency") == 3 and rc.get("frequency_schedule") == 7, rc)
check("an awareness ad group promotes nothing (no promotion type / pixel / page)",
      not any(k in rc for k in ("promotion_type", "pixel_id", "page_id", "optimization_event")), rc)
check("the fallback variant is exactly today's accepted lead-form shape (no target type, no ad-group page)",
      'and not (k == "page_id" and base_payload.get("promotion_target_type") == "INSTANT_PAGE")' in cp)
IDENT = {"identity_id": "I1", "identity_type": "CUSTOMIZED_USER"}
lib = ns["build_library_ad_payload"]({"template_name": "T", "ad_text": "hi", "destination_type": "website",
                                      "landing_page_url": "https://x.example", "_display_card_portfolio_id": "PF9"},
                                     "AG1", IDENT, "V1", "")["creatives"][0]
check("an uploaded-video ad keeps its display card (was silently dropped)", lib.get("card_id") == "PF9", lib)
aw = ns["build_ad_payload"]({"template_name": "T", "ad_text": " ", "destination_type": "none", "call_to_action": "LEARN_MORE"},
                            "AG1", {"identity_id": "S1", "identity_type": "BC_AUTH_TT", "item_id": "IT1"}, None)["creatives"][0]
check("an awareness ad has no URL, no page and no button", not any(k in aw for k in ("landing_page_url", "page_id", "call_to_action")), aw)

# ---------------------------------------------------------------------------------------
print("-- 6. warm-up --")
wsrc = read("app/warmup.py")
ns = {"LIVE": "AD_STATUS_DELIVERY_OK",
      "IN_REVIEW": {"AD_STATUS_IN_REVIEW", "AD_STATUS_AUDIT", "AD_STATUS_REAUDIT", "AD_STATUS_NOT_DELIVERY"},
      "STOPPED_TOKENS": ("CAMPAIGN_DISABLE", "ADGROUP_DISABLE", "AD_STATUS_DISABLE", "_DELETE"),
      "DEFAULT_BUDGET": 50.0, "MIN_BUDGET": 20.0}
exec(FUT + grab(wsrc, "decide") + "\n" + grab(wsrc, "clamp_budget"), ns)
d = ns["decide"]
check("approved & delivering → pause", d(["AD_STATUS_DELIVERY_OK"]) == "pause")
check("one ad live while another is still in review → pause (it's delivering)", d(["AD_STATUS_DELIVERY_OK", "AD_STATUS_AUDIT"]) == "pause")
check("still in review → keep waiting", d(["AD_STATUS_AUDIT"]) == "wait" and d([]) == "wait")
check("every ad refused → rejected (reported, not paused)", d(["AD_STATUS_AUDIT_DENY"]) == "rejected")
check("someone switched it off by hand → stop watching", d(["AD_STATUS_CAMPAIGN_DISABLE"]) == "stopped")
check("budget never below TikTok's $20 floor", ns["clamp_budget"](5) == 20.0 and ns["clamp_budget"](80) == 80.0 and ns["clamp_budget"]("x") == 50.0)

# the real recipe through the real synthesize()
for m in ("app.queries",):
    sys.modules.setdefault(m, types.ModuleType(m))
if "sqlalchemy" not in sys.modules:                 # warmup.py only needs the Session name
    sa = types.ModuleType("sqlalchemy"); sao = types.ModuleType("sqlalchemy.orm"); sao.Session = object
    sa.orm = sao; sys.modules["sqlalchemy"] = sa; sys.modules["sqlalchemy.orm"] = sao
class Tmpl:
    def __init__(self, **kw): self.__dict__.update(kw)
mm = types.ModuleType("app.models"); mm.Template = Tmpl; sys.modules["app.models"] = mm
setattr(pkg, "models", mm)
routes_pkg = types.ModuleType("app.routes"); routes_pkg.__path__ = [os.path.join(ROOT, "app", "routes")]; sys.modules["app.routes"] = routes_pkg
wu = importlib.import_module("app.warmup")
f = wu.warmup_fields(5)
check("the recipe is Reach · CPM · No-Bid with nothing to click through to",
      f["objective_type"] == "REACH" and f["optimization_goal"] == "REACH" and f["billing_event"] == "CPM"
      and f["bid_type"] == "BID_TYPE_NO_BID" and f["destination_type"] == "none", {k: f[k] for k in ("objective_type", "optimization_goal", "billing_event", "bid_type", "destination_type")})
check("a small daily budget per ad group, clamped to the floor", f["adgroup_budget"] == 20.0 and f["adgroup_budget_mode"] == "BUDGET_MODE_DAY")
check("targets each account's own country by default (no locations baked in)", f["account_default_geo"] is True and f["location_ids"] == [])
fl = wu.warmup_fields(60, own_country=False, location_ids=["3017382"])
check("…or an explicit country list", fl["account_default_geo"] is False and fl["location_ids"] == ["3017382"])
check("marked as a warm-up, TikTok placement only, downloads/shares off",
      f["_warmup"] is True and f["placement_auto"] is False and f["video_download_disabled"] and f["share_disabled"])
ns5 = {"datetime": datetime, "timezone": timezone, "models": lm, "acct_time": importlib.import_module("app.acct_time"), "_cta": lambda x: {"call_to_action": "LEARN_MORE"}}
exec(FUT + "ADULT_AGE_GROUPS = []\n" + grab(cp, "lead_gen_ages") + "\n" + grab(cp, "build_adgroup_payload"), ns5)
wp = ns5["build_adgroup_payload"]({**f, "location_ids": ["2921044"]}, None, "C1", 0, None, "")
check("the warm-up ad group TikTok receives: Reach, capped, no promotion, smooth, live",
      wp["optimization_goal"] == "REACH" and wp["frequency"] == 3 and "promotion_type" not in wp
      and wp["pacing"] == "PACING_MODE_SMOOTH" and wp["location_ids"] == ["2921044"] and wp["budget"] == 20.0, wp)

check("each account's own country is resolved per account BEFORE anything is created",
      cp.index('if fields.get("account_default_geo"):') < cp.index("# spark + pixel resolution BEFORE creating anything"))
check("an account whose country TikTok won't give: sent without a location, retried once if TikTok insists (v148)",
      '"_geo_unresolved": True' in cp and 'fields.get("_geo_unresolved") and "location_ids" not in ag_payload' in cp)
check("the launch record knows it's a warm-up and starts 'waiting'",
      'warmup=bool(fields.get("_warmup")), warmup_state="waiting" if fields.get("_warmup") else ""' in cp)
bg = read("app/background.py")
check("the sweep checks warm-ups (self-throttled, never breaks the sweep)",
      "warmup.poll(db)" in bg and 'log.exception("warm-up poll failed")' in bg and "POLL_EVERY_S = 120" in wsrc)
check("an approved warm-up is paused, its spend recorded, and the operator told",
      '"DISABLE")' in grab(wsrc, "poll") and 'lg.warmup_state, lg.warmup_done_at, lg.warmup_spend = "paused"' in wsrc
      and "was approved and is now paused" in wsrc)
check("one bad account never stops the pass", "except Exception:  # noqa: BLE001 — the sweep must survive one bad row" in wsrc)
wr = read("app/routes/warmup_page.py"); wt = read("app/templates/warmup.html")
check("the page launches through the ONE launch engine (result page, retries, errors)", "engine.queue_launch(db, f\"Warm-up" in wr)
check("only the workspace's own accounts and posts can be warmed up",
      "sc.allows(a)" in wr and "if row is None or not sc.owns(row)" in wr)
check("the watch list updates itself while anything is waiting (no reload)", 'fetch("/warmup/state.json"' in wt and "d.waiting > 0" in wt)
check("reuses our own pickers and look (no new UI kit)", "UI.pickAccounts(" in wt and "UI.pickProfileVideos(" in wt and 'class="card"' in wt)
nav = read("app/nav.py")
check("Warm up sits in the Launch section and ⌘K", '("/warmup", "Warm up")' in nav and '"/warmup": "launch"' in nav)
check("a live warm-up never raises the 'running without a source' error (no URL by design)",
      'if getattr(lg, "warmup", False):' in read("app/inbox.py"))
check("warm-up notices are titled and linked in the Inbox", '"warmup": "Warm-up"' in read("app/inbox.py") and 'return "/warmup", False' in read("app/inbox.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
