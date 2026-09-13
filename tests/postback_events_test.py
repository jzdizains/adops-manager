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

# ---- 4. a literal tracker token as source is "no source", not a campaign --------
check("'{trackingField3}' is a macro", pb._is_macro("{trackingField3}"))
check("a real campaign name is not", not pb._is_macro("JanisSlidesTest_095729_a8696"))
check("receiver blanks a literal-token source before the unattributed fallback",
      "if _is_macro(source):" in src and src.index("if _is_macro(source):") < src.index("source = UNATTRIBUTED"))

# ---- 5. P&L joins un-launched campaigns by TikTok campaign name ---------------
class _Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a, **k): return self
    def __iter__(self): return iter(self.rows)
class _DB:
    def __init__(self, rows): self.rows = rows
    def query(self, *cols): return _Q(self.rows)
class _Col:
    def in_(self, v): return v
models = sys.modules["app.models"]
models.CampaignRecord = types.SimpleNamespace(campaign_id=_Col(), campaign_name=_Col())
_mod("app.timeutil", local_date_str=lambda *a: "", timedelta=None)
pd = importlib.import_module("app.pnl_data")
db = _DB([("c1", "Manual_120000_a1111"), ("c2", "Manual_120000_a1111"), ("c3", "Other")])
got = pd.campaigns_named(db, ["Manual_120000_a1111", "Nope", ""])
check("name match returns every campaign with that name", got.get("Manual_120000_a1111") == ["c1", "c2"], str(got))
check("no lookup for empty sources", pd.campaigns_named(db, ["", None]) == {})
pp = open(os.path.join(ROOT, "app", "routes", "pnl_page.py")).read()
check("by-source table uses the name fallback only for sources with revenue", "campaigns_named(db, [s for s in rev_by_src if s not in cids_by_src])" in pp)
check("recent postbacks show a name match", '"via": "name"' in pp)
check("template labels name matches", "by campaign name (built outside this dashboard)" in tpl)

# ---- 6. campaign-id fallback (ClickFlare tracking field 4) ------------------
class _C:                       # a column stub: comparisons/desc/in_ just return something
    def __eq__(self, o): return True
    def __ne__(self, o): return True
    def desc(self): return self
    def in_(self, v): return v
    __hash__ = object.__hash__
class _Row:
    def __init__(self, **k): self.__dict__.update(k)
class _LQ:
    def __init__(self, rows, proj=None): self.rows, self.proj = rows, proj
    def filter(self, *a, **k): return self
    def filter_by(self, **k):
        self.rows = [r for r in self.rows if all(getattr(r, a, None) == v for a, v in k.items())]; return self
    def order_by(self, *a): return self
    def first(self):
        r = self.rows[0] if self.rows else None
        return (self.proj(r) if (r is not None and self.proj) else r)
class _CR:
    __name__ = "CampaignRecord"
    campaign_name = _C(); id = _C(); campaign_id = _C()
class _LL:
    __name__ = "LaunchLog"
    ok = _C(); campaign_id = _C(); source = _C(); id = _C()
models.CampaignRecord = _CR; models.LaunchLog = _LL
class _DB3:
    def __init__(self, logs, recs): self.logs, self.recs = logs, recs
    def query(self, *cols):
        if cols and cols[0] is _LL:
            return _LQ(list(self.logs))
        col = cols[0] if cols else None
        return _LQ(list(self.recs), proj=lambda r: (r.campaign_name,) if col is _CR.campaign_name else (r.id,))
logs = [_Row(ok=True, campaign_id="111", source="Launched_120000_a1111", id=1)]
recs = [_Row(id=9, campaign_id="222", campaign_name="Synced_130000_a2222"), _Row(id=10, campaign_id="111", campaign_name="Launched_120000_a1111")]
db3 = _DB3(logs, recs)
check("campaign id → launched source", pb.source_for_campaign_id(db3, "111") == "Launched_120000_a1111")
db_nolog = _DB3([], recs)
check("campaign id → synced campaign name when never launched", pb.source_for_campaign_id(db_nolog, "222") == "Synced_130000_a2222")
check("unknown campaign id → ''", pb.source_for_campaign_id(_DB3([], []), "999") == "")
check("receiver: cid used when source is empty", "if by_id and (not source or not source_known(db, source))" in src)
check("receiver: cid must be digits, never a macro", "cid_param.isdigit()" in src and "not _is_macro(cid_param)" in src)
sp = open(os.path.join(ROOT, "app", "routes", "settings_page.py")).read()
check("postback URL template carries &cid={trackingFieldN}", '"&cid={trackingField" + str(int(s.get("clickflare_cid_field") or 0)) + "}"' in sp)
check("…and omits it when the field is 0", 'if int(s.get("clickflare_cid_field") or 0) else ""' in sp)
ss2 = open(os.path.join(ROOT, "app", "settings_store.py")).read()
check("cid field defaults to 4 and allows 0 (off)", '"clickflare_cid_field": 4' in ss2 and 'int(4 if _cidf in (None, "") else _cidf), 0), 20)' in ss2)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
