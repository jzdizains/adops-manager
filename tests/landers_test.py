"""Landers (v124): pages the dashboard builds for the operator's own domains.

Functional (no fastapi/sqlalchemy needed for the builder):
- slug rules; config cleaning (defaults filled, bad colour / URL / rule reported); rules parsed
  from JSON with only known fields kept; public config carries no owner id
- build_html: one self-contained page — config baked as window.LANDER_CFG, runtime and
  pass-source inlined with the track host filled in, texts escaped, '</script>' in a text
  can't break out, pixel block only when a code is set
- package: zip with index.html + robots.txt
- funnel.accept: a built lander's slug is accepted with the new steps; unknown slugs and
  unknown steps are dropped; legacy start/play unchanged; bucket/inapp/os stored
Static: routes (page, save, delete, preview, package, public live config), public path,
nav, template pieces."""
import io, json, os, sys, types, zipfile

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

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
sa = _mod("sqlalchemy", func=_Any()); _mod("sqlalchemy.orm", Session=_Any); sa.orm = sys.modules["sqlalchemy.orm"]

class Lander:
    enabled = True
    def __init__(self, **kw):
        self.id = 1; self.owner_user_id = 7; self.template = "prelander"; self.name = ""; self.domain = ""; self.config = "{}"
        self.__dict__.update(kw)
class LanderEvent:
    rows = []
    def __init__(self, **kw): self.__dict__.update(kw); LanderEvent.rows.append(self)
class Col:
    def __init__(self, n): self.n = n
    def __eq__(self, o): return ("eq", self.n, o)
models = _mod("app.models", Lander=type("LanderModel", (), {"slug": Col("slug"), "enabled": Col("enabled")}), LanderEvent=LanderEvent)

import importlib
kit = importlib.import_module("app.landers")

print("-- slugs + config --")
check("slug rules", kit.valid_slug("open-uk") and kit.valid_slug("a1") and not kit.valid_slug("Open") and not kit.valid_slug("-x") and not kit.valid_slug("x" * 41) and not kit.valid_slug(""))
cfg, problems = kit.clean_config("prelander", {"headline": "Hi <b>there</b>", "next": "https://play.example.com/", "accent": "#123abc", "theme": "light",
                                                "escape_android": "intent-default", "escape_ios": "nope", "pixel": "D9I34-CBC!!", "pixel_event": "ClickButton",
                                                "rules": json.dumps([{"name": "iOS in app", "when": {"os": "ios", "inapp": "yes", "age": "18_24,25_34", "country": "us, gb", "hours": "9-17", "junk": 1}, "to": "https://a.example.com/"},
                                                                     {"when": {}, "to": "https://b.example.com/"}])})
check("defaults filled, values kept, escape validated per platform, pixel code stripped to alnum",
      not problems and cfg["headline"] == "Hi <b>there</b>" and cfg["button"] == "Continue" and cfg["theme"] == "light" and cfg["accent"] == "#123abc"
      and cfg["escape"] == {"android": "intent-default", "ios": "x-safari"} and cfg["pixel"] == "D9I34CBC" and cfg["pixel_event"] == "ClickButton", (cfg, problems))
check("rules: known fields only, countries upper-cased, order kept",
      cfg["rules"] == [{"name": "iOS in app", "to": "https://a.example.com/", "when": {"os": "ios", "inapp": "yes", "age": "18_24,25_34", "country": "US,GB", "hours": "9-17"}},
                       {"name": "", "to": "https://b.example.com/", "when": {}}], cfg["rules"])
_, p2 = kit.clean_config("prelander", {"next": "play.example.com", "accent": "purple", "rules": "[{\"to\": \"ftp://x\", \"when\": {\"os\": \"mac\", \"age\": \"u12\"}}]"})
check("problems reported in plain words", any("Next page" in x for x in p2) and any("Accent" in x for x in p2) and any("full http(s) URL" in x for x in p2), p2)
_, p3 = kit.clean_config("prelander", {})
check("no destination at all → told to set one", any("Set the next page" in x for x in p3), p3)
_, p4 = kit.clean_config("prelander", {"rules": "{bad"})
check("bad rules JSON reported", any("valid JSON" in x for x in p4), p4)
row = Lander(slug="open-uk", name="Open · UK", config=json.dumps(cfg))
pub = kit.public_config(row)
check("public config: routing only, no owner / texts", set(pub) == {"slug", "template", "next", "rules", "escape", "pixel_event", "enabled"} and "owner" not in json.dumps(pub) and pub["next"] == "https://play.example.com/")

print("-- build --")
cfg2 = dict(cfg); cfg2["text"] = "Tap </script><script>alert(1)</script> now"; cfg2["pixel"] = ""
row2 = Lander(slug="open-uk", config=json.dumps(cfg2))
html = kit.build_html(row2, "https://dash.example.com/", "sub1")
check("one page: doctype, baked config with slug + track host, runtime + pass-source inlined with the host filled",
      html.startswith("<!doctype html>") and 'window.LANDER_CFG = {' in html and '"slug": "open-uk"' in html and '"track_host": "https://dash.example.com"' in html
      and "w.L = w.L || {}" in html and "adops_pass" in html and 'https://dash.example.com/t/click' not in html and "__ADOPS_TRACK_HOST__" not in html and "__ADOPS_EXTRA_PARAMS__" not in html and '"sub1"' in html)
check("texts are escaped in the markup and the baked JSON can't close the script tag",
      "&lt;/script&gt;" in html and "</script><script>alert(1)" not in html.split("window.LANDER_CFG")[1].split("</script>")[0] and "<\\/script>" in html)
check("no pixel code → no pixel block", "analytics.tiktok.com" not in html and "ttq.load" not in html)
html_px = kit.build_html(Lander(slug="open-uk", config=json.dumps(cfg)), "https://dash.example.com")
check("pixel code → base code; page() comes from the runtime after identify(); the event only on the tap", "ttq.load('D9I34CBC');" in html_px and "ttq.load('D9I34CBC'); ttq.page();" not in html_px and "w.ttq.identify({ external_id: h })" in html_px and "L.pixel(cfg.pixel_event" in html_px and html_px.count("ttq.track(") == 1)
check("honest page: in a real browser it continues; in-app it escapes via L.escape; never a fake error / decoy / deep-link trap",
      "if (L.env.inapp) { L.escape(to); } else {" in html and "snssdk" not in html and "loading error" not in html.lower() and "popped" not in html)
check("theme + accent applied", 'data-theme="light"' in html and "--accent:#123abc" in html)
z = zipfile.ZipFile(io.BytesIO(kit.package(row2, "https://dash.example.com")))
check("package: index.html + robots.txt", set(z.namelist()) == {"index.html", "robots.txt"} and z.read("index.html").decode().startswith("<!doctype html>") and "Disallow" in z.read("robots.txt").decode())

print("-- funnel.accept --")
class Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a): return self
    def __iter__(self): return iter([("open-uk",), ("gone",)])
class DB:
    def query(self, *a): return Q([])
    def add(self, r): pass
fn = importlib.import_module("app.funnel")
db = DB()
ok, why = fn.accept(db, {"page": "open-uk", "step": "escaped", "vid": "v1", "bucket": "chrome-intent", "inapp": "tiktok", "os": "android", "source": "camp"})
check("a built lander's beacon is stored with bucket / in-app / os", ok and LanderEvent.rows[-1].page == "open-uk" and LanderEvent.rows[-1].step == "escaped" and LanderEvent.rows[-1].bucket == "chrome-intent" and LanderEvent.rows[-1].inapp == "tiktok" and LanderEvent.rows[-1].os == "android", (ok, why))
check("unknown slug dropped", fn.accept(db, {"page": "nope", "step": "view"})[0] is False)
check("unknown step dropped", fn.accept(db, {"page": "open-uk", "step": "purchase"})[0] is False)
check("legacy start/play unchanged", fn.accept(db, {"page": "start", "step": "view", "vid": "v2"})[0] and fn.accept(db, {"page": "start", "step": "escaped"})[0] is False)
check("slug list cached a minute", fn._slugs["at"] is not None and "open-uk" in fn._slugs["set"])

print("-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
rt = read("app/routes/landers.py"); au = read("app/routes/auth.py"); nv = read("app/nav.py"); th = read("app/templates/landers.html"); mn = read("app/main.py"); md = read("app/models.py"); js = read("app/static/lander.js")
check("routes: page, save (JSON to fetch), delete, preview, package, public live config", all(x in rt for x in ('@router.get("/landers")', '@router.post("/landers/save")', '/delete")', '/preview")', '/package.zip")', '@router.get("/t/l/{slug}.json")'))
      and "sc.owns(row)" in rt and 'Cache-Control": "no-store"' in rt)
check("live config public + only the routing fields", '"/t/l/"' in au and "kit.public_config(row)" in rt and "cf-ipcountry" in rt)
check("registered + in the nav", "landers_page.router" in mn and '("/landers", "Landers")' in nv and '"/landers": "creatives"' in nv)
check("model: Lander table, LanderEvent bucket/inapp/os", "class Lander(Base):" in md and '__tablename__ = "landers"' in md and "bucket = Column(String" in md.split("class LanderEvent")[1][:2600])
check("editor: rules rows, escape selects, pixel, live/baked split explained", "function ruleRow(r)" in th and 'name="escape_android"' in th and 'name="pixel_event"' in th and "re-download after changing" in th and "live — changes apply without re-uploading" in th)
check("runtime: env detection, live config with budget, beacon, pixel only on demand, rules, escape with in-app fallback (no fake error), age brackets",
      "L.detect = env" in js and "live_budget_ms" in js and "/t/lp" in js and "L.pixel = function" in js and "L.match = function" in js and "escape_miss" in js and "L.age = {" in js and "fake" not in js.lower().replace("never a fake error", ""))
check("STATIC_VERSION bumped", 'STATIC_VERSION = "157"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
