"""v155.15 — TikTok's Lead Generation Terms per ad account, through the official API (/term/check/,
/term/get/, /term/confirm/): the term type is discovered from TikTok's own refusal, the state is read,
and confirming happens ONLY from the operator's button."""
import ast, importlib, json, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

sys.modules.setdefault("app.diag", types.SimpleNamespace(record=lambda *a, **k: None))
lt = importlib.import_module("app.lead_terms")
api = lt.tiktok_api

print("-- pure --")
refusal = "term_type: value is not one of the allowed values, value is TIKTOK_LEAD_GEN_TERMS ,correct is InstantPage, LeadAds, Pixel, ReachFrequency"
check("LeadAds is tried first (TikTok's own list, 24 Sep)", lt.CANDIDATES[0] == "LeadAds")
check("the lead-gen term type is picked out of TikTok's refusal (its real wording)", lt.pick_type(("X",), refusal) == "LeadAds")
check("…and out of a quoted list", lt.pick_type(("X",), "must be one of ['Pixel', 'LEAD_GENERATION_TERMS_OF_SERVICE']") == "LEAD_GENERATION_TERMS_OF_SERVICE")
check("…nothing about leads in the list → nothing (never a guess)", lt.pick_type(lt.CANDIDATES, "must be one of ['A', 'B']") == "")
check("the confirmed flag is read whatever TikTok calls it",
      lt.confirmed_of({"is_confirmed": True}) is True and lt.confirmed_of({"status": "UNCONFIRMED"}) is False
      and lt.confirmed_of({"is_signed": 0}) is False and lt.confirmed_of({"foo": 1}) is None)

print("-- through a stand-in TikTok --")
state = {"types_ok": {"LeadAds"}, "confirmed": {"021"}, "calls": []}
settings = {}
sys.modules["app.queries"] = types.SimpleNamespace(get_setting=lambda db, k, d="": settings.get(k, d), set_setting=lambda db, k, v: settings.__setitem__(k, v))
def term_check(token, adv, tt):
    state["calls"].append(("check", adv, tt))
    if tt not in state["types_ok"]:
        raise api.TikTokError(40002, "term_type: value is not one of the allowed values, value is " + tt + " ,correct is " + ", ".join(sorted(state["types_ok"] | {"InstantPage", "Pixel"})))
    return {"is_confirmed": adv in state["confirmed"]}
def term_confirm(token, adv, tt):
    state["calls"].append(("confirm", adv, tt))
    state["confirmed"].add(adv)
    return {}
api.term_check, api.term_confirm = term_check, term_confirm
api.term_get = lambda token, adv, tt, lang="EN": {"term_content": "THE TERMS"}
lt._token = lambda db, adv: "tok"

check("the term type is settled on the first call and remembered", lt.term_type(None, "tok", "014") == "LeadAds"
      and settings["lead_term_type"] == "LeadAds" and len([c for c in state["calls"] if c[0] == "check"]) == 1)
settings.clear(); state["types_ok"] = {"LEAD_GENERATION_TERMS_OF_SERVICE"}; state["calls"].clear()
check("if TikTok ever renames it, the new name is read out of the refusal", lt.term_type(None, "tok", "014") == "LEAD_GENERATION_TERMS_OF_SERVICE")
settings.clear(); settings["lead_term_type"] = "LeadAds"; state["types_ok"] = {"LeadAds"}
state["calls"].clear()
check("status: confirmed / not confirmed", lt.status(None, "021") is True and lt.status(None, "014") is False)
check("…cached (no second TikTok call)", (lt.status(None, "021"), lt.status(None, "014")) == (True, False) and len(state["calls"]) == 2)
check("the Terms' text is fetched for the dialog", lt.text(None, "014") == "THE TERMS")
state["calls"].clear()
ok, msg = lt.accept(None, "014")
check("confirm: exactly one /term/confirm/ for that account, read back, remembered",
      ok and msg == "confirmed" and [c for c in state["calls"] if c[0] == "confirm"] == [("confirm", "014", "LeadAds")] and lt.status(None, "014") is True)
state["calls"].clear()
check("an already-confirmed account isn't confirmed again", lt.accept(None, "021") == (True, "already confirmed") and not [c for c in state["calls"] if c[0] == "confirm"])
def refuse(token, adv, tt): raise api.TikTokError(40002, "no")
api.term_confirm = refuse
state["confirmed"].discard("777")
ok, msg = lt.accept(None, "777")
check("TikTok refusing → not confirmed, with its answer", not ok and "40002" in msg)
lt._token = lambda db, adv: (_ for _ in ()).throw(lt.Unknown("that ad account isn't connected"))
check("no token → can't tell (None) / a clear message", lt.status(None, "x", fresh=True) is None and lt.accept(None, "x")[1] == "that ad account isn't connected")

print("-- wiring --")
ta = read("app/tiktok_api.py")
check("the three official endpoints", all(p in ta for p in ('"/term/check/"', '"/term/get/"', '"/term/confirm/"')))
lr = read("app/launch_review.py")
ns = {}
for n in ast.parse(lr).body:
    if (isinstance(n, ast.FunctionDef) and n.name in ("terms_cell", "verdict", "cell")) or \
       (isinstance(n, ast.Assign) and (ast.unparse(n.targets[0]).startswith("TERMS_") or ast.unparse(n.targets[0]).startswith("(OK, WARN"))):
        exec(compile(ast.Module([n], []), "lr", "exec"), ns)
tc = ns["terms_cell"]
check("review cell: confirmed / not confirmed (blocks, with the way out) / not checked (warns only)",
      tc(True)["state"] == ns["OK"] and tc(False)["state"] == ns["BAD"] and "Confirm" in tc(False)["hint"] and tc(None)["state"] == ns["WARN"])
check("an account without the Terms is blocked in Review — before anything is created",
      ns["verdict"]({"terms": tc(False), "geo": ns["cell"](ns["OK"], "Any")})[0].startswith("TikTok's Lead Generation Terms"))
cp = read("app/routes/campaigns.py")
check("the launch stops an Instant Form launch on such an account before creating anything",
      "_go, _why = _lt_terms.at_launch(db, acct.advertiser_id, fields.get(\"_launched_by\"))" in cp and cp.index("_lt_terms.at_launch(") < cp.index('trace.inflight("Smart+ campaign")')
      and "if not _go:\n                raise ConfigError(_why)" in cp)

print("-- confirming at launch, only when the launcher opted in (v155.17) --")
check("the switch exists and is OFF by default", '"lead_terms_auto": False' in read("app/settings_store.py"))
st = read("app/templates/settings.html")
check("Settings › Launch: a plain checkbox with the Terms' link, saying that ticking it means you've read them",
      'name="lead_terms_auto" {{ \'checked\' if s.lead_terms_auto }}' in st and "lead-gen-terms" in st.split('name="lead_terms_auto"')[1][:900]
      and "you have read" in st and st.index('id="leadterms"') > st.index('<section class="stab" data-tab="launch">'))
lt._token = lambda db, adv: "tok"
api.term_confirm = term_confirm
prefs = {"lead_terms_auto": False}
alerts = []
sys.modules["app.settings_store"] = types.SimpleNamespace(get_settings=lambda db, uid=None: prefs)
sys.modules["app.models"] = types.SimpleNamespace(Alert=lambda **kw: kw)
class _DB:
    def add(self, a): alerts.append(a)
    def commit(self): pass
    def rollback(self): pass
state["confirmed"].discard("014"); lt._cache.clear(); state["calls"].clear()
go, why = lt.at_launch(_DB(), "014", 1)
check("switch off: the launch stops before anything is created, pointing at the button and the switch",
      go is False and "Confirm Lead Generation Terms" in why and "Settings › Launch" in why and not [c for c in state["calls"] if c[0] == "confirm"])
prefs["lead_terms_auto"] = True
go, why = lt.at_launch(_DB(), "014", 1)
check("switch on: confirmed once, the launch goes on, and it is posted to the Inbox",
      go is True and [c for c in state["calls"] if c[0] == "confirm"] == [("confirm", "014", "LeadAds")] and len(alerts) == 1
      and alerts[0]["kind"] == "rule_action" and alerts[0]["ref_id"] == "014" and "confirmed" in alerts[0]["message"])
state["calls"].clear(); alerts.clear()
check("already confirmed: nothing to do, nothing posted", lt.at_launch(_DB(), "014", 1) == (True, "") and not state["calls"] and not alerts)
api.term_confirm = refuse
state["confirmed"].discard("555"); lt._cache.clear()
go, why = lt.at_launch(_DB(), "555", 1)
check("TikTok refusing the confirmation → the launch stops with its answer", go is False and "couldn't be confirmed" in why and "40002" in why)
check("Review: with the switch on a missing confirmation is a note, not a block",
      tc(False, auto=True)["state"] == ns["WARN"] and "confirmed at launch" in tc(False, auto=True)["text"] and tc(False)["state"] == ns["BAD"]
      and "auto=terms_auto" in lr and "terms_auto = terms_auto_for(db, fields.get(\"_launched_by\"))" in lr
      and "auto=auto" in cp.split("/campaigns/review/lead-terms.json")[1][:1200])
print("-- the quiet sweep (v155.18): ahead of any launch, only for users with the switch on --")
A = lambda adv, tok="t", st="ACTIVE": types.SimpleNamespace(advertiser_id=adv, access_token=tok, status=st)
now = 1_000_000.0
plan = lt.sweep_plan([A("1"), A("2", tok=""), A("3", st="Account suspended".upper()), A("4"), A("5")], {"4": now - 100, "5": now - 90000}, now, limit=2)
check("plan: connected, not suspended, not asked today; oldest first; a few per tick", plan == ["1", "5"], plan)
check("…asked-today accounts wait, nothing twice a day", lt.sweep_plan([A("4")], {"4": now - 100}, now) == [] and lt.SWEEP_TTL == 24 * 3600 and lt.SWEEP_PER_TICK <= 50)
users = [(types.SimpleNamespace(id=1), {"lead_terms_auto": True}, {"014", "021"}), (types.SimpleNamespace(id=2), {"lead_terms_auto": False}, {"555"})]
sys.modules["app.settings_store"] = types.SimpleNamespace(get_settings=lambda db, uid=None: prefs, per_user=lambda db: users)
class _Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a): return self
    def __iter__(self): return iter(self.rows)
class _DB2(_DB):
    def query(self, *a): return _Q([A("014"), A("021"), A("555")])
api.term_confirm = term_confirm
state["confirmed"] = {"021"}; lt._cache.clear(); lt._swept.clear(); state["calls"].clear(); alerts.clear()
M = types.SimpleNamespace(AdAccount=types.SimpleNamespace(advertiser_id=types.SimpleNamespace(in_=lambda x: x)), Alert=lambda **kw: kw)
n = lt.sweep(_DB2(), M)
check("user 1 (switch on): 014 confirmed, 021 left alone, Inbox line; user 2 (off): 555 never touched",
      n == 1 and [c for c in state["calls"] if c[0] == "confirm"] == [("confirm", "014", "LeadAds")] and "555" not in {c[1] for c in state["calls"]}
      and len(alerts) == 1 and alerts[0]["ref_id"] == "014", state["calls"])
state["calls"].clear()
check("the next tick asks nobody again today", lt.sweep(_DB2(), M) == 0 and state["calls"] == [])
bg = read("app/background.py")
check("runs on the slow sweep, errors contained", "_lt.sweep(db, _m)" in bg and '_sched.fail("lead_terms", _e)' in bg and bg.index("_lt.sweep(db, _m)") > bg.index("if slow:\n                try:"))
check("nothing else confirms by itself", read("app/lead_terms.py").count("accept(db, adv)") == 2 and "term_confirm(" not in bg)
check("routes: read a few at a time; the Terms' text; confirm only the viewer's accounts, >5 as a job",
      '@router.post("/campaigns/review/lead-terms.json")' in cp and '@router.get("/campaigns/lead-terms/text.json")' in cp
      and '@router.post("/campaigns/lead-terms/accept")' in cp and "if v and sc.allows(v)" in cp.split('"/campaigns/lead-terms/accept"')[1][:1500] and '"lead_terms_accept"' in cp)
check("the job exists", '@jobs.handler("lead_terms_accept")' in read("app/job_handlers.py") and '"lead_terms_accept"' in read("app/jobs.py"))
ui = read("app/static/ui.js")
check("one dialog for both buttons, showing TikTok's own Terms text before confirming",
      "UI.leadTermsConfirm = function" in ui and "/campaigns/lead-terms/text.json" in ui and "lead-gen-terms" in ui
      and "UI.leadTermsConfirm(" in read("app/static/launch-review.js") and "UI.leadTermsConfirm(" in read("app/templates/launch_result.html"))
check("nothing confirms by itself: accept() is only called from the confirm route, its job, and the opted-in launch",
      sum(read(p).count("lead_terms.accept(") for p in ("app/routes/campaigns.py", "app/job_handlers.py", "app/background.py", "app/launch_review.py")) == 2
      and "term_confirm(" not in read("app/background.py"))
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
