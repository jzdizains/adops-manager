"""Video upload when TikTok flags a flaw (v120).

/file/video/ad/upload/ with flaw_detect + auto_fix_enabled: on a flawed video TikTok
answers code 0 with `fix_task_id` + `flaw_types` and NO video_id (API reference) — the
launch used to die with "video upload returned no video_id". Now:
- that answer → one more upload with flaw detection OFF; the result carries flaw_types
- a plain code-0 answer without a video_id → the error message shows what TikTok said
- a normal answer → returned as before, no second upload
- the second upload never asks for flaw detection again (no loop)
- the launch records the flag (Diagnostics line + inbox alert) and goes on
Runs with httpx installed (tiktok_api imports it); app.config is stubbed."""
import json, os, sys, types

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
_mod("app.config", TIKTOK_API_BASE="https://business-api.tiktok.com/open_api/v1.3", TIKTOK_APP_ID="", TIKTOK_SECRET="", DIAG_MAX=100)
notes = []
_mod("app.diag", record=lambda *a, **k: notes.append(a))

import importlib
try:
    tk = importlib.import_module("app.tiktok_api")
except Exception as e:  # noqa: BLE001
    print("SKIP: app.tiktok_api can't import here:", e)
    sys.exit(0)

import tempfile
tmp = tempfile.NamedTemporaryFile("wb", suffix=".mp4", delete=False); tmp.write(b"\x00" * 5000); tmp.close()

class Resp:
    def __init__(self, body, url="https://business-api.tiktok.com/open_api/v1.3/file/video/ad/upload/"):
        self._b = body; self.status_code = 200; self.text = json.dumps(body); self.url = url
        self.request = types.SimpleNamespace(url=url, method="POST", headers={}, content=b"")
    def json(self): return self._b
calls = []
class Client:
    def __init__(self, answers): self.answers = list(answers)
    def post(self, url, headers=None, data=None, files=None, timeout=None):
        calls.append(dict(data))
        return Resp(json.loads(json.dumps(self.answers.pop(0))))     # a fresh body, as over the wire
def with_answers(*answers):
    calls.clear(); notes.clear()
    c = Client(answers)
    tk._client = lambda: c

FLAWED = {"code": 0, "message": "OK", "request_id": "r1", "data": [{"fix_task_id": "task-9", "flaw_types": ["VIDEO_RATIO", "BLACK_EDGE"]}]}
GOOD = {"code": 0, "message": "OK", "request_id": "r2", "data": [{"video_id": "v123", "poster_url": "https://p/x.jpg"}]}

print("\n-- flawed → re-upload without detection --")
with_answers(FLAWED, GOOD)
out = tk.upload_video_file("tok", "adv", tmp.name, "c1_clip.mp4")
check("returns the second upload's video_id", out.get("video_id") == "v123")
check("carries the flaw types along", out.get("flaw_types") == ["VIDEO_RATIO", "BLACK_EDGE"])
check("first call asked for flaw detection, second did not", calls[0].get("flaw_detect") == "true" and "flaw_detect" not in calls[1] and len(calls) == 2)
check("Diagnostics got the FLAW line naming the task and flaw types", any(n[2] == "FLAW" and "task-9" in n[3] and "VIDEO_RATIO" in n[3] for n in notes))

print("\n-- normal answer --")
with_answers(GOOD)
out = tk.upload_video_file("tok", "adv", tmp.name, "c1_clip.mp4")
check("one upload, video_id back, no flaw types", out.get("video_id") == "v123" and "flaw_types" not in out and len(calls) == 1)

print("\n-- code 0 without a video_id and without a fix task --")
with_answers({"code": 0, "message": "OK", "request_id": "r3", "data": [{"material_id": "m1"}]})
try:
    tk.upload_video_file("tok", "adv", tmp.name, "c1_clip.mp4"); err = None
except tk.TikTokError as e:
    err = e
check("raises APP with TikTok's actual answer in the message", err is not None and err.code == "APP" and "material_id" in str(err.message) and len(calls) == 1)

print("\n-- the fallback never loops --")
with_answers(FLAWED, FLAWED)
try:
    tk.upload_video_file("tok", "adv", tmp.name, "c1_clip.mp4"); err = None
except tk.TikTokError as e:
    err = e
check("second flawed answer (detection off) → error, exactly two uploads", err is not None and len(calls) == 2)

print("\n-- static --")
ca = open(os.path.join(ROOT, "app", "routes", "campaigns.py"), encoding="utf-8").read()
check("the launch records a flagged creative (Diagnostics + inbox alert) and goes on", 'if up.get("flaw_types"):' in ca and 'kind="creative_flaw"' in ca)
check("inbox knows the alert", '"creative_flaw"' in open(os.path.join(ROOT, "app", "inbox.py"), encoding="utf-8").read())
check("ILLEGAL_VIDEO_SIZE stops the launch before the campaign is built, marks the creative, names the minimums",
      'if any("VIDEO_SIZE" in str(f) for f in flaws):' in ca and 'creative.status = "error"' in ca and "540×960" in ca and 'raise tiktok_api.TikTokError("ASSET", why)' in ca)
em = open(os.path.join(ROOT, "app", "error_messages.py"), encoding="utf-8").read()
check("'Unsupported image size' explained; the fix button goes to Creatives, not Reconnect", 'unsupported image size' in em and '"label": "Open Creatives", "href": "/creatives", "why": "The video file is below' in em)
check("40002's friendly 'lacks permission' text no longer triggers the Reconnect button", '"permission" in raw' in em and '(code != "40002" and "permission" in low)' in em)
# behavioural: fix_for on a log shaped like the one on the result page
_mod("app.config", TIKTOK_API_BASE="x", TIKTOK_APP_ID="", TIKTOK_SECRET="", DIAG_MAX=100, APP_NAME="x")
try:
    emod = importlib.import_module("app.error_messages")
    log = types.SimpleNamespace(error_code="40002", error_message="TikTok rejected a field value — or this session lacks permission for this ad account.",
                                error_technical="code=40002 message=Unsupported image size. Please upload another image.", advertiser_id="1", spark_code_id=None, template_id=27)
    fx = emod.fix_for(log)
    check("fix_for → Open Creatives for this exact failure", fx is not None and fx["label"] == "Open Creatives", str(fx))
    log2 = types.SimpleNamespace(error_code="40002", error_message="TikTok rejected a field value — or this session lacks permission for this ad account.",
                                 error_technical="code=40002 message=targeting not valid", advertiser_id="1", spark_code_id=None, template_id=27)
    check("a plain 40002 field error offers no Reconnect button", emod.fix_for(log2) is None, str(emod.fix_for(log2)))
    log3 = types.SimpleNamespace(error_code="40002", error_message="…", error_technical="code=40002 message=No permission to operate advertiser", advertiser_id="1", spark_code_id=None, template_id=27)
    check("TikTok's own 'permission' wording still offers Reconnect", (emod.fix_for(log3) or {}).get("label") == "Reconnect the account")
except Exception as e:  # noqa: BLE001
    check("error_messages importable here", False, repr(e))

os.unlink(tmp.name)
print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
