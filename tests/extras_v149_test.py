"""v149 extras — alerts to Telegram / email, captions burned into videos (Studio), the Lab.

The caption burn runs FOR REAL when ffmpeg is on this machine (a generated clip, the caption
drawn by the real renderer, checked inside and outside its time window)."""
import importlib, io, os, shutil, subprocess, sys, tempfile, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

# =======================================================================================
print("-- alerts to Telegram / email --")
nt = importlib.import_module("app.notify")
A = lambda lvl, msg: types.SimpleNamespace(level=lvl, message=msg)
subj, text = nt.compose([A("err", "Account 123 suspended"), A("warn", "Wallet low")], "AdOpsX", "https://dash.example")
check("one message per batch, errors counted in the subject", subj == "AdOpsX: 2 alerts (1 error)" and "🔴 Account 123 suspended" in text and "🟠 Wallet low" in text)
check("…with a link to the Inbox", text.rstrip().endswith("https://dash.example/inbox"))
subj, text = nt.compose([A("err", f"e{i}") for i in range(20)])
check("long batches are cut with '…and N more'", text.count("🔴") == nt.MAX_LINES and "…and 5 more" in text)
check("levels: errors only, or errors + warnings", nt.levels_for("err") == ("err",) and nt.levels_for("warn") == ("err", "warn"))
import httpx
sent = []
def h(req):
    sent.append((str(req.url), req.content))
    return httpx.Response(200, json={"ok": True})
ok, err = nt.send_telegram("123:ABC", "-100777", "hi", client=httpx.Client(transport=httpx.MockTransport(h)))
check("Telegram: the bot API with the chat id", ok and sent[0][0] == "https://api.telegram.org/bot123:ABC/sendMessage" and b"-100777" in sent[0][1])
ok, err = nt.send_telegram("bad", "1", "x", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))))
check("…and says what Telegram answered when it refuses", not ok and "Unauthorized" in err)
os.environ.pop("SMTP_HOST", None)
check("email without SMTP says it isn't configured (no crash)", nt.send_email("a@b.c", "s", "t") == (False, "Email isn't configured on the server (SMTP_HOST / SMTP_FROM)."))
check("channels: only what's filled in", nt.channels({"notify_telegram_token": "t", "notify_telegram_chat": ""}) == {"telegram": False, "email": False}
      and nt.channels({"notify_telegram_token": "t", "notify_telegram_chat": "1", "notify_email_to": "x@y"}) == {"telegram": True, "email": True})
src = read("app/notify.py")
check("per user, only alerts that user can see (the Inbox rule), never replayed, pointer advanced before sending",
      "inbox.alert_visible(a, sc)" in src and "first time on: start from now" in src
      and src.index('queries.set_setting(db, key, str(top_id))  # advance first') < src.index("errs = deliver(us, subject, text)"))
check("the bot token is sealed at rest", 'SEALED_SETTINGS = ("events_access_token", "notify_telegram_token")' in read("app/secrets_box.py"))
check("the sweep sends them; Settings has an Alerts tab with a test button",
      "notify.dispatch(db)" in read("app/background.py") and 'data-tab="alerts"' in read("app/templates/settings.html")
      and '@router.post("/settings/notify/test")' in read("app/routes/settings_page.py"))

# =======================================================================================
print("-- captions burned into videos --")
vc = importlib.import_module("app.video_caption")
check("time window: clamped, ordered, 'to the end' = None", vc.clean_times("1.5", "", None) == (1.5, None) and vc.clean_times("3", "2", None) == (3.0, None)
      and vc.clean_times("-1", "abc", None) == (0.0, None) and vc.clean_times("2", "30", 10.0) == (2.0, None))
check("filter: whole clip, or only between start and end",
      vc.overlay_filter(0, None) == "[0:v][1:v]overlay=0:0:format=auto[v]" and "between(t,1.0,2.5)" in vc.overlay_filter(1.0, 2.5)
      and "between(t,2.0,1e9)" in vc.overlay_filter(2.0, None))
check("the video's shown size from ffmpeg's banner (no ffprobe on Render), rotation honoured",
      vc.dims_from_banner("Stream #0:0(und): Video: h264 (High), yuv420p, 1080x1920 [SAR 1:1 DAR 9:16], 30 fps") == (1080, 1920)
      and vc.dims_from_banner("Stream #0:0: Video: hevc, yuv420p, 1920x1080\n  displaymatrix: rotation of -90.00 degrees") == (1080, 1920))
if shutil.which("ffmpeg"):
    tmp = tempfile.mkdtemp()
    srcv, outv = os.path.join(tmp, "s.mp4"), os.path.join(tmp, "o.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", srcv], check=True)
    spec = {"text": "Caption test", "style": "outline", "size": "0.06", "y": "0.7"}
    vc.burn(srcv, outv, spec, 1.0, 2.0)
    from PIL import Image, ImageChops
    def region_diff(a, b):
        X, Y = Image.open(io.BytesIO(a)).convert("L"), Image.open(io.BytesIO(b)).convert("L")
        box = (0, int(640 * 0.6), 360, int(640 * 0.8))
        return sum(ImageChops.difference(X.crop(box), Y.crop(box)).histogram()[40:])
    check("REAL burn: the caption is there inside its window", region_diff(vc.frame(srcv, 1.5), vc.frame(outv, 1.5)) > 500)
    check("…and not outside it", region_diff(vc.frame(srcv, 0.5), vc.frame(outv, 0.5)) < 500)
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", outv], capture_output=True, text=True).stdout.split()
    check("…same size, audio kept", vc.dimensions(outv) == (360, 640) and "audio" in streams)
    check("the preview is a JPEG frame drawn by the same renderer", vc.preview(srcv, spec, 1.0)[:3] == b"\xff\xd8\xff")
else:
    print("SKIP real burn (no ffmpeg here)")
cr = read("app/routes/creatives.py"); cj = read("app/static/creatives.js")
check("a captioned COPY is queued (original untouched, same family), rendered by a slow-lane job, recovered after a restart",
      '"/creatives/{creative_id}/caption-video"' in cr and "text_parent_id=row.id" in cr and "source_md5=row.source_md5 or row.md5" in cr
      and '@jobs.handler("video_caption")' in read("app/job_handlers.py") and '"video_caption"' in read("app/jobs.py").split("SLOW_KINDS =")[1].split("LAUNCH_KINDS")[0]
      and "_vc.recover(_db, _vm, older_than_min=0)" in read("app/main.py"))
check("the freshen pass and the AI-recovery never touch caption rows", "src_path" not in cr[cr.index("def caption_video("):cr.index("# CAROUSELS")])
check("the drawer's 'Aa Caption' opens the editor with a live preview", "Aa Caption" in cj and "/caption-video/preview" in cj and "Burn into a copy" in cj)

# =======================================================================================
print("-- Lab --")
lab = importlib.import_module("app.lab")
check("batch refs from pasted text and result links", lab.refs("K7Q2M1, https://x.example/campaigns/result/K9A1B2?note=1  K7Q2M1; bad!ref") == ["K7Q2M1", "K9A1B2"])
a = lab.setup_of({"objective_type": "WEB_CONVERSIONS", "destination_type": "website", "adgroup_budget": 50, "location_ids": ["6252001"], "template_name": "P1"})
b = lab.setup_of({"objective_type": "WEB_CONVERSIONS", "destination_type": "instant_page", "adgroup_budget": 50, "location_ids": ["6252001"], "template_name": "P1"})
check("setup read from the batch recipe", a == {"Objective": "WEB_CONVERSIONS", "Destination": "website", "Budget": "50", "Countries": "6252001", "Preset": "P1"}, a)
check("an iteration shows exactly what it changed", lab.setup_diff(a, b) == ["Destination"])
check("roll-up: spend, revenue, profit, ROAS, live", lab.rollup([{"spend": 100, "revenue": 150, "live": True}, {"spend": 50, "revenue": 0}])
      == {"campaigns": 2, "live": 1, "spend": 150.0, "revenue": 150.0, "profit": 0.0, "roas": 1.0})
lp = read("app/routes/lab_page.py")
check("boards are per workspace; every edit checks ownership", lp.count("sc.owns(b)") == 1 and "_test(db, sc," in lp and "_board(db, sc," in lp)
check("a test's numbers are only the accounts in view", "sc.allows(lg.advertiser_id)" in read("app/lab.py"))
check("one revenue lookup per board, not per test", 'if cids and "pb" not in cache:' in read("app/lab.py"))
check("Lab sits under Launch (tab + ⌘K)", '("/lab", "Lab")' in read("app/nav.py") and "lab_page.router" in read("app/main.py"))
md = read("app/models.py")
check("LabBoard / LabTest tables", "class LabBoard(Base):" in md and "class LabTest(Base):" in md)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
