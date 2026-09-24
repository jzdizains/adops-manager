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
refusal = "term_type: value must be one of ['TIKTOK_ADS_TERMS', 'LEAD_GENERATION_TERMS_OF_SERVICE', 'PANGLE_TERMS']"
check("the lead-gen term type is picked out of TikTok's refusal", lt.pick_type(lt.CANDIDATES, refusal) == "LEAD_GENERATION_TERMS_OF_SERVICE")
check("…nothing about leads in the list → nothing (never a guess)", lt.pick_type(lt.CANDIDATES, "must be one of ['A', 'B']") == "")
check("the confirmed flag is read whatever TikTok calls it",
      lt.confirmed_of({"is_confirmed": True}) is True and lt.confirmed_of({"status": "UNCONFIRMED"}) is False
      and lt.confirmed_of({"is_signed": 0}) is False and lt.confirmed_of({"foo": 1}) is None)

print("-- through a stand-in TikTok --")
state = {"types_ok": {"LEAD_GENERATION_TERMS_OF_SERVICE"}, "confirmed": {"021"}, "calls": []}
settings = {}
sys.modules["app.queries"] = types.SimpleNamespace(get_setting=lambda db, k, d="": settings.get(k, d), set_setting=lambda db, k, v: settings.__setitem__(k, v))
def term_check(token, adv, tt):
    state["calls"].append(("check", adv, tt))
    if tt not in state["types_ok"]:
        raise api.TikTokError(40002, refusal)
    return {"is_confirmed": adv in state["confirmed"]}
def term_confirm(token, adv, tt):
    state["calls"].append(("confirm", adv, tt))
    state["confirmed"].add(adv)
    return {}
api.term_check, api.term_confirm = term_check, term_confirm
api.term_get = lambda token, adv, tt, lang="EN": {"term_content": "THE TERMS"}
lt._token = lambda db, adv: "tok"

check("the term type is discovered once from the refusal and remembered", lt.term_type(None, "tok", "014") == "LEAD_GENERATION_TERMS_OF_SERVICE"
      and settings["lead_term_type"] == "LEAD_GENERATION_TERMS_OF_SERVICE" and len([c for c in state["calls"] if c[0] == "check"]) == 1)
state["calls"].clear()
check("status: confirmed / not confirmed", lt.status(None, "021") is True and lt.status(None, "014") is False)
check("…cached (no second TikTok call)", (lt.status(None, "021"), lt.status(None, "014")) == (True, False) and len(state["calls"]) == 2)
check("the Terms' text is fetched for the dialog", lt.text(None, "014") == "THE TERMS")
state["calls"].clear()
ok, msg = lt.accept(None, "014")
check("confirm: exactly one /term/confirm/ for that account, read back, remembered",
      ok and msg == "confirmed" and [c for c in state["calls"] if c[0] == "confirm"] == [("confirm", "014", "LEAD_GENERATION_TERMS_OF_SERVICE")] and lt.status(None, "014") is True)
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
       (isinstance(n, ast.Assign) and ("TERMS_NOT" in ast.unparse(n.targets[0]) or ast.unparse(n.targets[0]).startswith("(OK, WARN"))):
        exec(compile(ast.Module([n], []), "lr", "exec"), ns)
tc = ns["terms_cell"]
check("review cell: confirmed / not confirmed (blocks, with the way out) / not checked (warns only)",
      tc(True)["state"] == ns["OK"] and tc(False)["state"] == ns["BAD"] and "Confirm" in tc(False)["hint"] and tc(None)["state"] == ns["WARN"])
check("an account without the Terms is blocked in Review — before anything is created",
      ns["verdict"]({"terms": tc(False), "geo": ns["cell"](ns["OK"], "Any")})[0].startswith("TikTok's Lead Generation Terms"))
cp = read("app/routes/campaigns.py")
check("the launch stops an Instant Form launch on such an account before creating anything",
      "if _lt_terms.status(db, acct.advertiser_id) is False:" in cp and cp.index("if _lt_terms.status(db, acct.advertiser_id) is False:") < cp.index('trace.inflight("Smart+ campaign")'))
check("routes: read a few at a time; the Terms' text; confirm only the viewer's accounts, >5 as a job",
      '@router.post("/campaigns/review/lead-terms.json")' in cp and '@router.get("/campaigns/lead-terms/text.json")' in cp
      and '@router.post("/campaigns/lead-terms/accept")' in cp and "if v and sc.allows(v)" in cp.split('"/campaigns/lead-terms/accept"')[1][:1500] and '"lead_terms_accept"' in cp)
check("the job exists", '@jobs.handler("lead_terms_accept")' in read("app/job_handlers.py") and '"lead_terms_accept"' in read("app/jobs.py"))
ui = read("app/static/ui.js")
check("one dialog for both buttons, showing TikTok's own Terms text before confirming",
      "UI.leadTermsConfirm = function" in ui and "/campaigns/lead-terms/text.json" in ui and "lead-gen-terms" in ui
      and "UI.leadTermsConfirm(" in read("app/static/launch-review.js") and "UI.leadTermsConfirm(" in read("app/templates/launch_result.html"))
check("nothing confirms by itself: accept() is only called from the confirm route and its job",
      sum(read(p).count("lead_terms.accept(") for p in ("app/routes/campaigns.py", "app/job_handlers.py", "app/background.py", "app/launch_review.py")) == 2
      and "term_confirm(" not in read("app/background.py"))
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
