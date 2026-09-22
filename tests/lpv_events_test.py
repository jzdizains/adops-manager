"""Landing-view conversions (v127): a server-side Events API event per lander view.

Functional (stubbed TikTok + settings): the request-time gate; the queue never blocks and
drops over the cap; fire(): off / page not listed / no ttclid / our click id resolved to the
ttclid / missing token / sent with the right payload (event, fixed value, dedupe id per
visitor + page, hashed visitor id, ip, ua, page url, referrer). Static: the /t/lp hook,
settings keys + cleaning, the settings block, beacons carry the page URL."""
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

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
_mod("app.routes"); sys.modules["app.routes"].__path__ = [os.path.join(ROOT, "app", "routes")]
calls = []
class TikTokError(Exception):
    def __init__(self, code, message): super().__init__(message); self.code, self.message = code, message
def track_event(token, pixel, **kw):
    calls.append({"token": token, "pixel": pixel, **kw})
    if token == "bad":
        raise TikTokError(40001, "no permission")
    return {"code": 0}
_mod("app.tiktok_api", TikTokError=TikTokError, track_event=track_event)
class Click:
    def __init__(self, ttclid): self.ttclid = ttclid
_mod("app.tracking", is_click_id=lambda v: len(v or "") == 12 and v.isalnum(), lookup=lambda db, cid: Click("E.C.P.fromclick") if cid == "abcdefghijkl" else None)
SETTINGS = {"lpv_enabled": True, "lpv_event": "CompleteRegistration", "lpv_value": 6.0, "lpv_pages": "play, open-uk", "events_access_token": "tok", "events_currency": "USD", "events_test_code": ""}
_mod("app.settings_store", for_account=lambda db, adv: SETTINGS, get_settings=lambda db, uid=None: SETTINGS)
import hashlib
_mod("app.routes.postback", _launch_for_source=lambda db, src: None, _events_pixel_code=lambda db, src, s: ("acct-token", "PIXEL1"),
     hash_id=lambda v: hashlib.sha256(v.strip().lower().encode()).hexdigest() if v else "", page_url_for=lambda db, src, s: "https://fallback.landing/")
_mod("app.database", SessionLocal=lambda: types.SimpleNamespace(close=lambda: None))

import importlib
lpv = importlib.import_module("app.lpv_events")

print("-- gate + queue --")
check("only a VIEW with a visitor id and a click id is wanted", lpv.wanted({"step": "view", "vid": "v1", "ttclid": "E.C.P.x"}) and not lpv.wanted({"step": "engaged", "vid": "v1", "ttclid": "E.C.P.x"})
      and not lpv.wanted({"step": "view", "vid": "", "ttclid": "E.C.P.x"}) and not lpv.wanted({"step": "view", "vid": "v1", "ttclid": 1}) and not lpv.wanted({"step": "view", "vid": "v1", "ttclid": ""}))
lpv._ensure_worker = lambda: None          # keep the thread out of the test
check("enqueue keeps the fields, trimmed, and counts", lpv.enqueue({"step": "view", "vid": "v1", "ttclid": "E.C.P.x", "page": "play", "source": "camp", "url": "https://l/?a=1", "ref": "https://www.tiktok.com/"}, "1.2.3.4", "UA") and lpv.STATS["queued"] == 1 and lpv._q.qsize() == 1)
item = lpv._q.get()
check("queued item shape", item["ip"] == "1.2.3.4" and item["ttp"] == "" and item["ua"] == "UA" and item["url"] == "https://l/?a=1" and item["page"] == "play")
old_q = lpv._q; lpv._q = __import__("queue").Queue(maxsize=1)
lpv.enqueue({"step": "view", "vid": "v1", "ttclid": "E.C.P.x"}, "", ""); ok2 = lpv.enqueue({"step": "view", "vid": "v2", "ttclid": "E.C.P.y"}, "", "")
check("over the cap: dropped and counted, never blocks", ok2 is False and lpv.STATS["dropped"] == 1)
lpv._q = old_q

print("-- fire --")
base = {"page": "play", "source": "camp", "vid": "vid-1", "ttclid": "E.C.P.x", "url": "https://l/", "ref": "https://www.tiktok.com/", "ip": "1.2.3.4", "ua": "UA", "ttp": "ttp-cookie-1"}
r = lpv.fire(None, dict(base))
check("sent: event, fixed value, dedupe id per visitor (same id on /start and /play), hashed visitor id, ip, ua, page url, referrer, settings token wins",
      r == "sent CompleteRegistration" and calls[-1]["event"] == "CompleteRegistration" and calls[-1]["value"] == 6.0 and calls[-1]["event_id"] == "lpv-vid-1"
      and calls[-1]["external_id"] == hashlib.sha256(b"vid-1").hexdigest() and calls[-1]["ip"] == "1.2.3.4" and calls[-1]["user_agent"] == "UA" and calls[-1]["page_url"] == "https://l/"
      and calls[-1]["referrer"] == "https://www.tiktok.com/" and calls[-1]["ttclid"] == "E.C.P.x" and calls[-1]["ttp"] == "ttp-cookie-1" and calls[-1]["token"] == "tok" and calls[-1]["pixel"] == "PIXEL1", (r, calls[-1]))
lpv._settings_cache.clear(); SETTINGS["lpv_pages"] = "open-uk"
check("page not listed → skipped", lpv.fire(None, dict(base)).startswith("skipped: page"))
lpv._settings_cache.clear(); SETTINGS["lpv_pages"] = ""
check("empty pages = every page", lpv.fire(None, dict(base)) == "sent CompleteRegistration")
check("our 12-char click id is resolved to the TikTok click id", lpv.fire(None, {**base, "ttclid": "abcdefghijkl"}) == "sent CompleteRegistration" and calls[-1]["ttclid"] == "E.C.P.fromclick")
check("unknown click id → skipped, nothing sent", lpv.fire(None, {**base, "ttclid": "zzzzzzzzzzzz"}).startswith("skipped: no ttclid"))
check("no page url on the beacon → the launch's / settings' landing URL", lpv.fire(None, {**base, "url": ""}) == "sent CompleteRegistration" and calls[-1]["page_url"] == "https://fallback.landing/")
lpv._settings_cache.clear(); SETTINGS["lpv_enabled"] = False
check("off → skipped", lpv.fire(None, dict(base)) == "skipped: off")
lpv._settings_cache.clear(); SETTINGS["lpv_enabled"] = True; SETTINGS["events_access_token"] = "bad"
check("TikTok error → error string, no raise", lpv.fire(None, dict(base)).startswith("error: code 40001"))
lpv._settings_cache.clear(); SETTINGS["events_access_token"] = ""
check("no settings token → the account's token", lpv.fire(None, dict(base)) == "sent CompleteRegistration" and calls[-1]["token"] == "acct-token")
check("pages_match is case-insensitive and trims", lpv.pages_match("Play , Open-UK", "play") and lpv.pages_match("Play , Open-UK", "OPEN-UK") and not lpv.pages_match("play", "start") and lpv.pages_match("", "anything"))

print("-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
tr = read("app/routes/tracking.py"); ss = read("app/settings_store.py"); st = read("app/templates/settings.html"); lj = read("app/static/lander.js")
check("/t/lp hands VIEW beacons to the queue after storing them (background, beacon answers at once)", 'if d.get("step") == "view":' in tr and "lpv_events.enqueue(d, _ip(request), request.headers.get(\"user-agent\", \"\"))" in tr and "[:4000]" in tr)
check("settings keys + cleaning (event name pattern, pages list)", '"lpv_enabled": False' in ss and '"lpv_value": 6.0' in ss and '"lpv_pages": "play"' in ss and 'clean["lpv_event"] = "CompleteRegistration"' in ss and 'clean["lpv_pages"] = ' in ss)
check("settings UI block with stats + the note about what it does to optimisation", 'name="lpv_enabled"' in st and 'name="lpv_event"' in st and 'name="lpv_value"' in st and 'name="lpv_pages"' in st and "lpv_stats.sent" in st and "optimises for visits" in st)
check("beacons carry the page url + referrer (kit runtime and the tikmobileplay pages)", "url: String(location.href || \"\").slice(0, 900), ref: (d.referrer" in lj)
lander = os.environ.get("LANDER_DIR") or os.path.join(os.path.dirname(ROOT), "lander")
play = os.path.join(lander, "play", "index.html")
if os.path.exists(play):
    ph = open(play, encoding="utf-8").read()
    check("tikmobileplay v9 beacon: ttclid value, referrer, url, _ttp cookie", 'url: String(location.href || "").slice(0, 900), ttp: (function () {' in ph and '_ttp=([^;]+)' in ph)
check("STATIC_VERSION bumped", 'STATIC_VERSION = "144"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
