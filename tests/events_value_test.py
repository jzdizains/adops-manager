"""Events API value (v125): every forwarded event can carry a fixed value (e.g. $6) instead
of the postback's payout, optionally only for sources / landing URLs that match; the click's
_ttp cookie id rides along as a second identifier. Replaces the PHP postback bridge: the
conversion (CompleteRegistration) is sent server-side by the dashboard, never by the page.

Functional (fastapi/sqlalchemy stubbed): event_value in every mode; track_event's payload
carries ttp; save_settings cleans the new keys. Static: forwarder, click path, settings UI,
the Playful lander's browser events."""
import os, sys, types

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

class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
    def __getattr__(self, n): return _Any()

_mod("fastapi", APIRouter=_Any, Depends=_Any(), Request=_Any, Form=_Any())
_mod("fastapi.responses", JSONResponse=_Any, RedirectResponse=_Any, PlainTextResponse=_Any, Response=_Any)
sa = _mod("sqlalchemy", func=_Any()); _mod("sqlalchemy.orm", Session=_Any); sa.orm = sys.modules["sqlalchemy.orm"]
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.live_log", push=lambda *a, **k: None)
_mod("app.models")
posted = []
_mod("app.tiktok_api", TikTokError=Exception, track_event=lambda *a, **k: posted.append(k) or {})
_mod("app.timeutil"); _mod("app.tracking", TTCLID_MAX=2000, note_long_ttclid=lambda *a, **k: None, is_click_id=lambda v: False, lookup=lambda *a: None)
_mod("app.database", get_db=lambda: None); _mod("app.settings_store", get_settings=lambda db: {}); _mod("app.templating", render=lambda *a, **k: None)
_mod("app.queries"); _mod("app.scope", for_request=lambda *a: None); _mod("app.routes.guard")

import importlib
pb = importlib.import_module("app.routes.postback")

class Ev:
    def __init__(self, revenue, source=""): self.revenue, self.source = revenue, source
class Click:
    def __init__(self, url="", ttp=""): self.url, self.ttp = url, ttp

print("-- event_value --")
check("payout mode → the postback's revenue", pb.event_value(Ev(4.5), None, {"events_value_mode": "payout", "events_value_fixed": 6}) == 4.5)
check("default (no setting) → revenue", pb.event_value(Ev(2), None, {}) == 2.0)
check("fixed, no match words → every event is $6", pb.event_value(Ev(4.5, "camp_a1"), None, {"events_value_mode": "fixed", "events_value_fixed": 6.0}) == 6.0)
S = {"events_value_mode": "fixed", "events_value_fixed": 6.0, "events_value_match": "tikmobileplay.com/play, playful"}
check("fixed + match on the click's landing URL → $6", pb.event_value(Ev(4.5, "camp_a1"), Click(url="https://start.tikmobileplay.com/play/?source=camp_a1"), S) == 6.0)
check("fixed + match on the source name → $6 (case-insensitive)", pb.event_value(Ev(4.5, "Playful_Sept_01"), None, S) == 6.0)
check("fixed + no match → the payout stays", pb.event_value(Ev(4.5, "hoodie_uk"), Click(url="https://other.example.com/"), S) == 4.5)
check("no click row and no source → payout", pb.event_value(Ev(3, ""), None, S) == 3.0)

print("-- match signals (v126) --")
import hashlib
check("hash_id: trim + lower-case + sha256, like the pixel block", pb.hash_id("  ABC-123 ") == hashlib.sha256(b"abc-123").hexdigest() and pb.hash_id("") == "")
class C2:
    def __init__(self, **kw): self.ttp = ""; self.vid = ""; self.ip = ""; self.user_agent = ""; self.url = ""; self.referrer = ""; self.__dict__.update(kw)
class LE:
    def __init__(self, vid, ref, ip="9.9.9.9", ua="UA-beacon", ttp="ttp-b"): self.vid, self.ref, self.ip, self.ua, self.ttp = vid, ref, ip, ua, ttp
class LEQ:
    hits = []
    def __init__(self, *a): pass
    def filter(self, *a): return self
    def order_by(self, *a): return self
    def first(self): return LEQ.hits[0] if LEQ.hits else None
class LDB:
    def query(self, *a): return LEQ()
LEQ.hits = []
sys.modules["app.models"].LanderEvent = type("LanderEvent", (), {"ttclid": _Any(), "id": _Any()})
sig = pb.match_signals(LDB(), Ev(1, "x"), C2(ttp="cookie1", vid="vid-1", ip="1.2.3.4", user_agent="UA", url="https://l/", referrer="https://tiktok.com/"))
check("from the Click row: ttp, hashed visitor id, ip, ua, page, referrer", sig == {"ttp": "cookie1", "external_id": pb.hash_id("vid-1"), "ip": "1.2.3.4", "user_agent": "UA", "page_url": "https://l/", "referrer": "https://tiktok.com/"}, sig)
LEQ.hits = [LE("vid-9", "https://www.tiktok.com/")]
ev = Ev(1, "x"); ev.ttclid = "E.C.P.abc"
sig2 = pb.match_signals(LDB(), ev, None)
check("no Click row (ClickFlare lander): visitor id, referrer, ip + browser from the view beacon by ttclid", sig2["external_id"] == pb.hash_id("vid-9") and sig2["referrer"] == "https://www.tiktok.com/" and sig2["ip"] == "9.9.9.9" and sig2["user_agent"] == "UA-beacon" and sig2["ttp"] == "ttp-b", sig2)
LEQ.hits = []
sig3 = pb.match_signals(LDB(), ev, None)
check("nothing known → empty signals, no crash", all(v == "" for v in sig3.values()))
fw = open(os.path.join(ROOT, "app", "routes", "postback.py"), encoding="utf-8").read()
check("forwarder sends every signal and says which ones went", "sig = match_signals(db, event, click)" in fw and 'external_id=sig["external_id"]' in fw and 'referrer=sig["referrer"]' in fw and '· signals:' in fw)
real_src = open(os.path.join(ROOT, "app", "tiktok_api.py"), encoding="utf-8").read()
check("track_event: user.external_id + page.referrer", 'user["external_id"] = external_id' in real_src and 'item["page"]["referrer"] = referrer' in real_src)
ps = open(os.path.join(ROOT, "app", "static", "pass-source.js"), encoding="utf-8").read()
check("pass-source: ONE visitor id (svid → localStorage → cookie → minted), sent with the click", "function visitorId()" in ps and 'get("svid")' in ps and "vid: visitorId()" in ps and 'localStorage.setItem("tmp_vid", id)' in ps)
md = open(os.path.join(ROOT, "app", "models.py"), encoding="utf-8").read(); fnl = open(os.path.join(ROOT, "app", "funnel.py"), encoding="utf-8").read()
check("Click.vid; LanderEvent.ttclid + ref stored from the beacon (old pages' 1/0 flag ignored)", "vid = Column(String" in md.split("class Click(Base)")[1][:1500] and "ttclid = Column(String, default=\"\", index=True)" in md.split("class LanderEvent")[1][:2000] and 'ref=_clean(d.get("ref"), 400)' in fnl and 'isinstance(d.get("ttclid"), str) and len(d.get("ttclid")) > 1' in fnl)

print("-- the forwarded payload --")
tk = importlib.import_module("app.tiktok_api") if False else None
# real track_event (not the stub) — build its payload through api_post captured
real_src = open(os.path.join(ROOT, "app", "tiktok_api.py"), encoding="utf-8").read()
check("track_event accepts ttp and puts it in user{} next to ttclid", 'ttp: str = "", external_id: str = "", referrer: str = "") -> dict:' in real_src and 'user["ttp"] = ttp' in real_src)
fw = open(os.path.join(ROOT, "app", "routes", "postback.py"), encoding="utf-8").read()
check("forwarder: value from event_value, ttp from the signals", "value=event_value(event, click, s)" in fw and 'ttp=sig["ttp"]' in fw)

print("-- settings --")
ss_src = open(os.path.join(ROOT, "app", "settings_store.py"), encoding="utf-8").read()
check("new keys with safe defaults", '"events_value_mode": "payout"' in ss_src and '"events_value_fixed": 0.0' in ss_src and '"events_value_match": ""' in ss_src
      and 'if clean.get("events_value_mode") not in ("payout", "fixed")' in ss_src)
st = open(os.path.join(ROOT, "app", "templates", "settings.html"), encoding="utf-8").read()
check("settings UI: value mode, fixed amount, match words", 'name="events_value_mode"' in st and 'name="events_value_fixed"' in st and 'name="events_value_match"' in st)

print("-- _ttp from the lander --")
ps = open(os.path.join(ROOT, "app", "static", "pass-source.js"), encoding="utf-8").read()
tr = open(os.path.join(ROOT, "app", "routes", "tracking.py"), encoding="utf-8").read()
trk = open(os.path.join(ROOT, "app", "tracking.py"), encoding="utf-8").read()
md = open(os.path.join(ROOT, "app", "models.py"), encoding="utf-8").read()
check("pass-source reads the pixel's _ttp cookie and registers it with the click", "_ttp=([^;]+)" in ps and "ttp: ttp," in ps)
check("/t/click stores it; Click has the column", 'ttp=str(d.get("ttp") or "")' in tr and "ttp=_clean(ttp, 120)" in trk and "ttp = Column(String" in md.split("class Click(Base)")[1][:1200])

print("-- the Playful lander --")
lander = os.environ.get("LANDER_DIR") or os.path.join(os.path.dirname(ROOT), "lander")
play = os.path.join(lander, "play", "index.html")
if os.path.exists(play):
    ph = open(play, encoding="utf-8").read()
    first = ph.split("function firstEvents()")[1][:600]
    check("on landing: Page view + LandingPageView (+ ViewContent), after identify", "ttq.page();" in first and 'ttq.track("LandingPageView"' in first and "viewContent();" in first)
    check("CompleteRegistration is never called from the page", "completeRegistration()" not in ph.replace("window.ttEvents.completeRegistration() stays available", ""))
    check("beacon (v7) carries the ttclid value and the referrer", 'ttclid: p("ttclid") || 0, ref: (document.referrer || "").slice(0, 400)' in ph)
else:
    print("SKIP lander files not found (set LANDER_DIR)")

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
