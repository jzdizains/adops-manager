"""Pixel / postback audit fixes (v96).

1. The postback URL carries this dashboard's postback password (`key=`). It was stored
   verbatim in every PostbackEvent.raw_query and rendered on the P&L page's "raw" button.
   A credential must never be stored with the event — strip_key() masks it before the row
   is written, and the template filter masks rows written before this fix.
2. A campaign optimising for a BROWSER-fired event (ClickButton / ViewContent — the
   lander's own pixel sends those) must not have its postback mirrored as that same event:
   the browser copy and the server copy carry different event_ids, so TikTok counts the
   click twice and never hears about the conversion. The postback fires the conversion
   event from Settings instead.
3. test_event_code must look like a TikTok test code; an email or a sentence would flag
   every forwarded event as a test event (never counted for optimisation).

Runs without fastapi/sqlalchemy (stubbed).
"""
import sys, types, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

# ---- stubs -----------------------------------------------------------------
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
    def __getattr__(self, n): return _Any()

fastapi = _mod("fastapi", APIRouter=_Any, Depends=_Any(), Request=_Any)
_mod("fastapi.responses", JSONResponse=_Any)
sa = _mod("sqlalchemy", func=_Any())
_mod("sqlalchemy.orm", Session=_Any)
sa.orm = sys.modules["sqlalchemy.orm"]

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.live_log", push=lambda *a, **k: None)
_mod("app.models")
_mod("app.tiktok_api", TikTokError=Exception, track_event=lambda *a, **k: {})
_mod("app.timeutil")
_mod("app.tracking", TTCLID_MAX=2000, note_long_ttclid=lambda *a, **k: None, is_click_id=lambda v: False, lookup=lambda *a: None)
_mod("app.database", get_db=lambda: None)
_mod("app.settings_store", get_settings=lambda db: {})
_mod("app.templating", render=lambda *a, **k: None)

import importlib
pb = importlib.import_module("app.routes.postback")

# ---- 1. the key never reaches the stored row ---------------------------------
q = "source=camp_a1~abc&payout=5&key=6350ecaf1416608982f807c72dea5984&txn=t1"
out = pb.strip_key(q)
check("key masked in the middle of the query", "6350ecaf" not in out and "key=***" in out, out)
check("other params untouched", out.startswith("source=camp_a1~abc&payout=5&") and out.endswith("&txn=t1"), out)
check("key first in the query", pb.strip_key("key=secret&a=1") == "key=***&a=1")
check("key last in the query", pb.strip_key("a=1&key=secret") == "a=1&key=***")
check("no key → unchanged", pb.strip_key("a=1&b=2") == "a=1&b=2")
check("'monkey=' is not the key", pb.strip_key("monkey=1&key=s") == "monkey=1&key=***")
check("empty is safe", pb.strip_key("") == "" and pb.strip_key(None) == "")
src = open(os.path.join(ROOT, "app", "routes", "postback.py")).read()
check("the row is written through strip_key", "raw_query=strip_key(str(request.url.query))" in src)
tpl = open(os.path.join(ROOT, "app", "templates", "pnl.html")).read()
check("the P&L raw button masks old rows too", 'data-raw="{{ e.raw_query|strip_key }}"' in tpl)
tp = open(os.path.join(ROOT, "app", "templating.py")).read()
check("strip_key registered as a template filter", '"strip_key": _strip_key' in tp)

# ---- 2. browser-fired optimisation events -----------------------------------
class Log:
    def __init__(self, opt): self.optimization_event = opt
S = {"events_event_mode": "campaign", "events_event_name": "CompleteRegistration"}
ev, how = pb.event_name_for(None, "camp", S, log=Log("BUTTON"))
check("BUTTON campaign: postback fires the conversion event, not ClickButton", ev == "CompleteRegistration", ev)
check("…and says why", "fires itself" in how, how)
ev, how = pb.event_name_for(None, "camp", S, log=Log("ON_WEB_DETAIL"))
check("ViewContent campaign: same rule", ev == "CompleteRegistration", ev)
ev, how = pb.event_name_for(None, "camp", S, log=Log("ON_WEB_REGISTER"))
check("registration campaign still mirrors its own event", ev == "CompleteRegistration" and how.startswith("campaign optimises for ON_WEB_REGISTER"), how)
ev, how = pb.event_name_for(None, "camp", S, log=Log("SHOPPING"))
check("purchase campaign → Purchase (Events API 2.0 name)", ev == "Purchase", ev)
ev, how = pb.event_name_for(None, "camp", dict(S, events_event_name="CompletePayment"), log=Log("BUTTON"))
check("legacy fixed name normalised on the BUTTON path", ev == "Purchase", ev)
ev, how = pb.event_name_for(None, "camp", dict(S, events_event_mode="fixed"), log=Log("SHOPPING"))
check("fixed mode ignores the campaign", ev == "CompleteRegistration" and how == "fixed")

# ---- 3. test_event_code sanity (the regex is what settings_store applies) -----
ss = open(os.path.join(ROOT, "app", "settings_store.py")).read()
m = re.search(r're\.fullmatch\(r"([^"]+)", str\(clean\.get\("events_test_code"\)', ss)
check("settings_store validates events_test_code", bool(m))
if m:
    rx = m.group(1)
    ok = lambda v: bool(re.fullmatch(rx, v))
    check("TEST1234 accepted", ok("TEST1234"))
    check("an email is rejected", not ok("janis@glitchy.com"))
    check("a sentence is rejected", not ok("my test code"))
    check("64 chars accepted, 65 rejected", ok("a" * 64) and not ok("a" * 65))
    check("empty handled by the caller (falls to '')", not ok(""))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
