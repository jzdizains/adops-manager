"""Instant Page builder (v120): a one-button page built on an account by driving
TikTok's own builder in a headless browser.

Functional (real Playwright against a LOCAL fake of the builder screen that uses the
exact labels TikTok shows — Create, Customize, "Untitled page", "Call to action button",
"Button Text", "View website", the URL placeholder, the two checkboxes, Complete):
- the run names the page, sets the button text + URL, honours the checkboxes, Completes
- screenshots before/after Complete exist
- a page that asks for verification / login stops the build with challenge=True
- the memory guard refuses to open a browser above the ceiling (retry flag)
Static: template model owned per user; routes (save/delete/build/build-bc/screenshot
scoped to the viewer's accounts); slow-lane job; preset carries page_template_id; launch
builds-if-missing only on a "has no instant page" miss and re-verifies via /page/get/.
Skips the browser part when Playwright/Chromium isn't available here."""
import http.server, json, os, socketserver, sys, threading, types
from pathlib import Path

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

SCRATCH = Path(os.environ.get("PB_SCRATCH") or "/tmp/adops_pb_test")
SCRATCH.mkdir(parents=True, exist_ok=True)
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
_mod("app.config", DATA_DIR=SCRATCH)
_mod("app.spark_web_api", load_cookies=lambda: {"sessionid_ads": "x", "csrftoken": "c"})
rss = {"v": 100.0}
_mod("app.background", rss_mb=lambda: rss["v"], mem_limit_mb=lambda: 0)
import importlib
ipb = importlib.import_module("app.instant_page_builder")

FAKE = """<!doctype html><html><head><meta charset="utf-8"><title>TikTok Ads Manager</title></head><body>
<h1>Instant Page</h1><button id="create">Create</button>
<div id="tpl" hidden><div onclick="openBuilder()">Customize</div><div>Products for sale</div></div>
<div id="builder" hidden>
  <div><span id="title" onclick="editTitle()">Untitled page 9/17/26, 00:56</span> <svg id="pencil" width="12" height="12" onclick="editTitle()"><rect width="12" height="12"/></svg></div>
  <input id="titleBox" hidden onkeydown="if(event.key==='Enter'){document.getElementById('title').textContent=this.value;this.hidden=true;}">
  <h3>Settings</h3>
  <div onclick="document.getElementById('cta').hidden=false"><b>Call to action button</b><div>Direct users to carry out an action</div></div>
  <div id="cta" hidden>
    <button type="button">Text</button><button type="button">Image</button>
    <div>Button Text</div><input id="btntext" value="Continue">
    <div>Destination URL (Required)</div>
    <label><input type="radio" name="dest" checked> View website</label><label><input type="radio" name="dest"> Install App</label>
    <input id="url" placeholder="Please provide the URL of your website beginning with http://">
    <label><input type="checkbox" id="cur" checked> Show hand cursor</label>
    <label><input type="checkbox" id="bot" checked> Set the button to the bottom of the page</label>
  </div>
  <button id="save">Save</button><button id="complete" onclick="complete()">Complete</button>
</div>
<pre id="saved" hidden></pre>
<script>
document.getElementById('create').onclick=function(){document.getElementById('tpl').hidden=false;};
function openBuilder(){document.getElementById('tpl').hidden=true;document.getElementById('builder').hidden=false;}
function editTitle(){var b=document.getElementById('titleBox');b.hidden=false;b.value=document.getElementById('title').textContent;b.focus();}
function complete(){var s={name:document.getElementById('title').textContent,button:document.getElementById('btntext').value,url:document.getElementById('url').value,cur:document.getElementById('cur').checked,bot:document.getElementById('bot').checked};
 document.getElementById('builder').hidden=true;var p=document.getElementById('saved');p.hidden=false;p.textContent=JSON.stringify(s);
 fetch('/saved', {method:'POST', body: JSON.stringify(s)});}
</script></body></html>"""
CHALLENGE = "<html><body><h1>Security check</h1><p>Please verify you are human</p><button>Create</button></body></html>"
BLANK = "<html><head><title>TikTok Ads Manager</title></head><body><script>console.error('boot failed: x')</script></body></html>"

saved = {}
class H(http.server.BaseHTTPRequestHandler):
    mode = "ok"
    def log_message(self, *a): pass
    def do_GET(self):
        body = {"challenge": CHALLENGE, "blank": BLANK}.get(H.mode, FAKE).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        saved.update(json.loads(self.rfile.read(n) or b"{}"))
        self.send_response(204); self.end_headers()

print("\n-- browser --")
if not ipb.available():
    print("SKIP: Playwright not installed here")
else:
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    tpl = types.SimpleNamespace(name="FC UK", button_text="Continue now", url="https://example.com/lp?source=__CAMPAIGN_NAME__", button_color="", hand_cursor=True, bottom_fixed=False)
    try:
        r = ipb.build("7659328211721060370", tpl, base_url=base)
        check("the run completes every step", r["ok"] is True and [s["step"] for s in r["steps"] if s["ok"]][:4] == ["open the Instant Page library", "Create → Customize", "name the page", "call-to-action button"], json.dumps(r)[:400])
        check("the page got the template's name, button text and URL", saved.get("name") == "FC UK" and saved.get("button") == "Continue now" and saved.get("url") == tpl.url, str(saved))
        check("checkboxes honoured (hand cursor on, bottom off)", saved.get("cur") is True and saved.get("bot") is False, str(saved))
        shots = [s["shot"] for s in r["steps"] if s.get("shot")]
        check("screenshots before and after Complete exist", len(shots) == 2 and all((ipb.SHOT_DIR / s).exists() for s in shots))
        H.mode = "challenge"
        r2 = ipb.build("7659328211721060370", tpl, base_url=base)
        check("a verification page stops the build and says so", r2["ok"] is False and r2["challenge"] is True and "verification" in r2["error"].lower(), r2.get("error"))
        H.mode = "blank"; old_wait = ipb.LIBRARY_WAIT_S; ipb.LIBRARY_WAIT_S = 3
        try:
            r5 = ipb.build("7659328211721060370", tpl, base_url=base)
        finally:
            ipb.LIBRARY_WAIT_S = old_wait
        check("a blank library page reports HTTP status, title, HTML size, frames and console errors", r5["ok"] is False
              and "HTTP 200" in r5["error"] and "TikTok Ads Manager" in r5["error"] and "1 frame(s)" in r5["error"] and "boot failed" in r5["error"], r5.get("error"))
        H.mode = "ok"; rss["v"] = 400.0
        r3 = ipb.build("7659328211721060370", tpl, base_url=base)
        check("memory guard: no browser above the ceiling, marked retry", r3["ok"] is False and r3.get("retry") is True and "memory" in r3["error"].lower())
        rss["v"] = 100.0
        # no Chromium on the machine: install once, retry once, then report — never crash
        old_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        empty = SCRATCH / "no-browsers"; empty.mkdir(exist_ok=True)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(empty)
        calls = []
        real_install = ipb.install_browser
        ipb.install_browser = lambda timeout=600: calls.append(1) or ""
        try:
            r4 = ipb.build("7659328211721060370", tpl, base_url=base)
            check("missing Chromium → one install attempt, one retry, honest error", len(calls) == 1 and r4["ok"] is False and "Chromium isn't installed" in r4["error"] and any(s["step"].startswith("installed Chromium") for s in r4["steps"]), json.dumps(r4)[:300])
        finally:
            ipb.install_browser = real_install
            if old_env is None:
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            else:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = old_env
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            print("SKIP: Chromium not installed here:", msg.splitlines()[0][:100])
        else:
            check("browser run raised", False, msg[:200])
    finally:
        srv.shutdown()

print("\n-- static --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
check("looks_like_challenge ignores the builder's own words", not ipb.looks_like_challenge("Call to action button · Button Text · log in to continue") and ipb.looks_like_challenge("Security check — please verify"))
ms = read("app/models.py")
check("PageTemplate model, owned per user, in OWNED_MODELS", "class PageTemplate(Base)" in ms and 'owner_user_id = Column(Integer, nullable=True, index=True, default=ctx.owner_default)' in ms.split("class PageTemplate")[1][:600] and '"PageTemplate"' in read("app/scope.py"))
ip = read("app/routes/instant_pages.py")
check("routes: save / delete / build one / build BC", all(x in ip for x in ('@router.post("/instant-pages/templates/save")', '/templates/{tpl_id}/delete', '/templates/{tpl_id}/build")', '/templates/{tpl_id}/build-bc")')))
check("templates are workspace-scoped (owns / owner_for_new)", "return t if (t is not None and sc.owns(t)) else None" in ip and "models.PageTemplate(owner_user_id=sc.owner_for_new)" in ip)
check("BC build targets only accounts in view without the page", "if a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id not in have" in ip)
check("screenshot route: only for an account in the viewer's workspace", 'if not scope_mod.for_request(request, db).allows(m.group(1)):' in ip)
check("a challenge or the memory guard stops the whole run", 'if r.get("challenge"):' in ip and 'if r.get("retry"):' in ip)
check("job in the slow lane with progress", '@jobs.handler("instant_page_build")' in read("app/job_handlers.py") and '"instant_page_build"' in read("app/jobs.py").split("SLOW_KINDS = {")[1][:60])
check("preset stores page_template_id; synthesize carries it", '"page_template_id": int(val("page_template_id"))' in read("app/routes/templates_routes.py") and 'fields["page_template_id"] = int(s.get("page_template_id") or 0) or None' in read("app/routes/launch.py"))
ca = read("app/routes/campaigns.py")
check("launch builds only on a 'has no instant page' miss, then uses the verified page id", 'except AssetResolveError as miss:' in ca and '"has no instant page" not in str(miss).lower()' in ca and 'r = ipb.build_and_verify(db, acct, tpl)' in ca and 'fields["instant_page_id"] = r["page_id"]' in ca)
check("Chromium lives on the data disk and is installed on first use", 'BROWSERS_DIR = config.DATA_DIR / "pw-browsers"' in read("app/instant_page_builder.py") and '"-m", "playwright", "install", "chromium"' in read("app/instant_page_builder.py") and "except BrowserMissing" in read("app/instant_page_builder.py"))
check("verification is the official API, retried briefly", "ip.sync_account(db, acct)" in read("app/instant_page_builder.py") and "time.sleep(3)" in read("app/instant_page_builder.py"))
th = read("app/templates/instant_pages.html"); tf = read("app/templates/template_form.html")
check("Instant Pages: templates card, form pop-up, build → reusable account pop-up → build-multi",
      'id="templates"' in th and 'id="tplFormBox"' in th and 'tpl-build" type="button"' in th
      and "UI.pickAccounts({" in th and '/build-multi' in th and 'i.name = "target_ids"' in th)
check("preset form: template picker", 'name="page_template_id"' in tf and "page_templates" in tf)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
