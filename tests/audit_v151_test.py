"""v151 audit — every bug the full review found, pinned so it can't come back.

Behaviour is run for real where the code imports without the web stack; the rest are
source checks on the exact fix."""
import importlib, json, os, sys, types
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
def grab(src, name):
    i = src.index("def " + name + "(")
    ends = [e for e in (src.find(m, i + 1) for m in ("\ndef ", "\n@", "\nclass ", "\n# ----", "\n\n\n")) if e != -1]
    return src[i:(min(ends) if ends else len(src))]

# =======================================================================================
print("-- crashes --")
ns = {"timezone": timezone}
exec(grab(read("app/issues.py"), "issue_since"), ns)
aware = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
naive = datetime(2026, 9, 3, 12, 0)
check("issue scan: an aware status-change time next to naive ones no longer raises",
      ns["issue_since"](None, aware, None, naive) == datetime(2026, 9, 1, 12, 0))
check("…and the status listener stamps naive UTC", "target.status_changed_at = utcnow().replace(tzinfo=None)" in read("app/models.py"))
check("account drawer: the note is its text, not the row (JSON can't serialise a model)",
      '"note": (lambda n: n.text if n else "")(activity_mod.get_note(db, "account", a.advertiser_id))' in read("app/routes/dashboard.py"))
check("Lab board: no call to a macro that only exists on another page", "money0(" not in read("app/templates/lab_board.html"))
for _m in ("fastapi", "fastapi.responses"):
    if _m not in sys.modules:
        _mod = types.ModuleType(_m); _mod.Request = object; _mod.RedirectResponse = object; sys.modules[_m] = _mod
sg = importlib.import_module("app.security_gate")
cfg = importlib.import_module("app.config")
cfg.SECURITY_PIN = "4711"
check("PIN: a non-ASCII entry is simply wrong (was a 500)", sg.check_pin("é1") is False and sg.check_pin("4711") is True)
for k in range(sg.PIN_MAX_FAILS):
    sg.pin_failed("u1|ip")
check("PIN: locked after 5 wrong tries, per person", sg.pin_locked("u1|ip") > 0 and sg.pin_locked("u2|ip") == 0)
check("postback key / OAuth state / signed links / trusted device compare bytes (non-ASCII can't 500)",
      "compare_digest(str(k).encode(), str(key).encode())" in read("app/settings_store.py")
      and "compare_digest(str(got).encode(), str(expected).encode())" in read("app/routes/oauth.py")
      and "compare_digest(str(sig).encode()" in read("app/routes/pub.py"))
check("logout always clears the browser session, even if the database is busy",
      "    finally:\n        request.session.clear()" in read("app/routes/auth.py"))

# =======================================================================================
print("-- secrets --")
check("httpx request lines never reach the logs (the Telegram token is in its URL)",
      'for _quiet in ("httpx", "httpcore"):' in read("app/main.py"))
st = read("app/templates/settings.html")
check("tokens are write-only in Settings (never rendered back into the page)",
      'name="notify_telegram_token" value=""' in st and 'name="events_access_token" value=""' in st
      and "{{ s.notify_telegram_token }}" not in st and "{{ s.events_access_token }}" not in st)
sp = read("app/routes/settings_page.py")
check("…an empty field keeps the saved token; 'remove it' clears it",
      'SECRET_FIELDS = ("notify_telegram_token", "events_access_token")' in sp and 'form.get("clear_" + key)' in sp)
check("key rotation: an old SESSION_SECRET in SECRETS_KEY_OLD opens old secrets and recovery codes",
      'keys += [k, f"session:{k}"]' in read("app/secrets_box.py") and "_old_secrets()" in read("app/auth_security.py"))
tp = read("app/templating.py")
ns = {}
exec(tp[tp.index("def _js"):tp.index("templates.env.filters.update")].replace("from markupsafe import Markup", "Markup = str"), ns)
out = ns["_js"](json.dumps({"t": "</script><img src=x onerror=alert(1)>"}))
check("JSON in <script> can't be broken out of (</script> in a preset's text)", "</script>" not in out and json.loads(out)["t"].startswith("</script>"))
import glob, re
check("…and every *_json blob uses it", not any(re.search(r"_json\s*\|\s*safe", open(f).read()) for f in glob.glob(os.path.join(ROOT, "app/templates/*.html"))))
check("Lab board title escaped in the drawer subtitle", "sub: UI.esc(D.board.title)" in read("app/templates/lab_board.html"))

# =======================================================================================
print("-- workspaces --")
cp = read("app/routes/campaigns.py")
check("a launch builds lead forms only from ITS workspace's form template",
      "models.FormTemplate.owner_user_id == _owner" in cp and "models.FormTemplate.owner_user_id == owner" in read("app/launch_review.py"))
check("a page is only copied from an account of the same workspace",
      "by_adv[r.owner_advertiser_id].owner_user_id == acct.owner_user_id" in cp)
check("Create Campaign refuses another workspace's spark / creative id",
      "if sp is None or not sc.owns(sp):" in cp and "if cr is None or not sc.owns(cr):" in cp)
check("'Launch again on…' only replays a batch of this workspace, with its own posts / creatives",
      "That batch isn't in your workspace." in cp and "if int(v) in own_sp]" in cp)
dsh = read("app/routes/dashboard.py")
check("Business Center drawer only lists accounts in view", "if a.status != \"ACCESS_LOST\" and sc.allows(a.advertiser_id)]" in dsh)
ip = read("app/routes/instant_pages.py"); lf = read("app/routes/lead_forms.py")
check("Instant Page / Lead Form sync: only accounts in view, as a background job",
      '"asset_sync"' in ip and "sc.allows(a.advertiser_id)" in ip.split('@router.post("/instant-pages/sync")')[1][:900]
      and '"asset_sync"' in lf and '@jobs.handler("asset_sync")' in read("app/job_handlers.py") and '"asset_sync"}' in read("app/jobs.py"))
check("forms TikTok no longer lists are dropped (only on a full API answer)", "for row in [] if not from_api else" in lf)
lab = read("app/lab.py")
check("Lab: revenue and batch recipes of this workspace only", "advertiser_ids=(sc.ids if sc is not None else None)" in lab
      and "seen = next((r for r in rs if any(lg.batch_ref == r for lg in logs)), None)" in lab
      and "if r[0] and sc.allows(r[1])" in read("app/routes/lab_page.py"))

# =======================================================================================
print("-- launches never doubled --")
check("'Retry failed' never re-offers accounts an earlier retry of the batch took (or is taking)",
      "covered, busy = retry_covered(db, batch_ref)" in cp and "_note_retry(db, batch_ref, new_ref)" in cp)
qw = read("app/queue_worker.py")
check("queue items are claimed atomically (sweep + Process now can't both launch one)",
      '.update({models.LaunchQueueItem.status: "running"}, synchronize_session=False)' in qw and "if not claimed:" in qw
      and "_RUN.acquire(blocking=False)" in qw)
check("…an item never sticks on 'running' after an error", 'it.status, it.last_error = "failed", f"queue error:' in qw)
au = read("app/routes/automation.py")
check("queue Retry refuses items that may have left a campaign behind",
      au.count("queue_worker.retry_safe(db, item)") == 2 and "threading.Thread(target=_go" in au)
check("a launch never builds a page / form that the build queue is building right now",
      '_ab.active_for(db, models, "page", acct.advertiser_id, name)' in cp and '_ab.active_for(db, models, "form", acct.advertiser_id, name)' in cp)
check("the resumed campaign's name is kept on the trace (P&L join)", 'trace.campaign(campaign_id, reused=True, name=' in cp)

# =======================================================================================
print("-- build queue / forms --")
ab = read("app/asset_builds.py")
check("waiting builds get their job back (boot + housekeeping); dead cookies / Stop close the rest with a reason",
      "ensure_running(db, models)" in ab and "_close_pending(db, models, \"failed\"" in ab and "_close_pending(db, models, \"cancelled\"" in ab
      and "ensure_running" in read("app/background.py"))
lfb = importlib.import_module("app.lead_form_builder")
calls = []
fake_web = types.SimpleNamespace(
    read_page=lambda *a, **k: ({"code": 0, "data": {"page_info": {"data": "{}", "business_type": 1}}}, "s", []),
    _ok=lambda r: True, explain=lambda r, t: "x", thumb_uri=lambda p: "", POLL_TRIES=1, POLL_S=0,
    _post=lambda path, body, t: (calls.append(path) or {"code": 0, "data": {"page_id": "9"}}))
sys.modules["app.instant_page_web"] = fake_web
import app as _app
_app.instant_page_web = fake_web
lfb.rewrite_form_fields = lambda data, edits: ("{}", ["question_options"])
lfb.extract_form_fields = lambda data, title="": {"question_options": ["a", "b", "c", "d"]}
r = lfb.build_form("1", "Offer", "2", {"question_options": ["x", "y", "z"]})
check("a form template with a different number of answers than its master is refused BEFORE anything is created",
      not r["ok"] and "4 answers" in r["error"] and "nothing was created" in r["error"] and calls == [])
check("a form that fails its read-back is renamed 'FAILED – …' so no launch picks it by name",
      "_quarantine(web, form_id, name, new_data" in read("app/lead_form_builder.py") and 'FAILED_PREFIX = "FAILED – "' in read("app/lead_form_builder.py"))
vc = read("app/video_caption.py")
check("captions still waiting in the queue survive a restart; a failed render rolls back and leaves no orphan file",
      "if r.id in waiting:" in vc and "db.rollback()                    # the failure may BE a commit" in vc and "os.unlink(out)" in vc)

# =======================================================================================
print("-- alerts / automation --")
nt = read("app/notify.py")
check("alerts: only rows up to the pass's top id; every row looked at in pages; off keeps the pointer moving; one user's error never stops the rest",
      "models.Alert.id <= top_id" in nt and "while cur < top_id and scanned < SCAN_MAX" in nt
      and "off: keep up" in nt and "one user's bad address / token never stops the rest" in nt)
check("rule alerts belong to the ACCOUNT (a campaign id hid them from every buyer's Inbox and alerts)",
      'kind="rule_action", ref_id=rec.campaign_id' not in read("app/rules.py"))
check("a breach held by the hourly cap gets its turn next hour (not next day)",
      'if last.action == "held":' in read("app/rules.py"))
check("switching stocking off mid-run sticks", "def save_run(db, user_id, st: dict)" in read("app/page_stock.py")
      and "switched off while this pass ran" in read("app/page_stock.py"))
sk = importlib.import_module("app.spark_check")
check("an expired ACCESS token never brands a good spark code 'bad'",
      sk.classify(40001, "Access token has expired") == "transient" and sk.classify(40002, "The auth code has expired") == "bad")
check("…and a code another worker just took isn't checked twice", "# never one another worker has just taken" in read("app/spark_check.py"))
mn = read("app/main.py")
check("'last seen' stamps no longer empty the sign-in cache for everyone", "if not _only_seen(target):" in mn)
check("a failed trace recovery rolls back before the next sweep step", "db.rollback()                        # a failed commit must not poison" in read("app/background.py"))

# =======================================================================================
print("-- geo policy edge --")
gf = importlib.import_module("app.geo_fit")
gf.country_resolver = lambda db, models: (lambda loc: None)          # regions never synced
check("locations we can't place are 'somewhere else', never 'no countries' (US-only accounts stay for US launches)",
      gf.wanted_countries(None, None, ["3469034"]) == [gf.UNKNOWN_NON_US]
      and not gf.policy_fit("us_only", None, gf.wanted_countries(None, None, ["3469034"]))[0]
      and gf.policy_fit("us_only", None, gf.wanted_countries(None, None, ["6252001"]))[0])
check("review / auto-pick read every account's countries in one go", "def targetable_map(" in read("app/geo_fit.py")
      and "known_map = geo_fit.targetable_map(db, models, ids)" in read("app/launch_review.py"))
check("the review endpoints run off the event loop", "await run_in_threadpool(_launch_review, request, db, form)" in cp
      and "return await run_in_threadpool(work)" in cp)

# =======================================================================================
print("-- front end --")
css = read("app/static/style.css")
check("pop-overs / toasts / ⌘K sit above drawers and modals",
      ".pop { position: absolute; z-index: 1100;" in css and ".toasts { position: fixed; right: 16px; bottom: 16px; z-index: 1250;" in css
      and ".cmdk-bg { position: fixed; inset: 0; z-index: 1150;" in read("app/static/topnav.css"))
check("no iOS zoom on inputs; drawers fit the phone screen; sticky headers can stick",
      "input, select, textarea { font-size: 16px !important; }" in css and "height: 100dvh;" in css and ".table-scroll { overflow-x: auto; max-height: 75vh; overflow-y: auto; }" in css)
allsrc = "".join(open(f).read() for f in glob.glob(os.path.join(ROOT, "app/static/*.css")) + glob.glob(os.path.join(ROOT, "app/static/*.js")) + glob.glob(os.path.join(ROOT, "app/templates/*.html")))
check("no undefined CSS variables (--muted, --line, --danger)", not re.search(r"var\(--(muted|line|danger)\b", allsrc))
check("no page reloads itself any more (only the launch result, once, when it finishes)",
      re.findall(r"location\.reload\(\)", allsrc) == ["location.reload()"] and "if (!live) { location.reload(); return; }" in read("app/templates/launch_result.html"))
jb = read("app/templates/jobs.html")
check("Jobs: progress patched in place, list swapped in place, paused when hidden", "function refresh()" in jb and "document.hidden" in jb and "location.reload" not in jb)
check("creatives: bulk / drawer actions update in place (Archive's Undo survives)", "function softRefresh()" in read("app/static/creatives.js"))
check("background jobs offer fresh results instead of yanking the page away", "UI.staleBanner" in read("app/templates/creators.html")
      and "UI.staleBanner" in read("app/templates/pixels.html") and "UI.staleBanner = function" in read("app/static/ui.js"))
abj = read("app/static/asset-builds.js")
check("build queue: a blip never ends the live view; paused while hidden; per-BC select honours the search",
      "M.fails = (M.fails || 0) + 1" in abj and "visibilitychange" in abj and "shown(g, a) && pred(g, a)" in abj)
check("no double saves (form templates, Lab tests, Lab boards)",
      "one click = one template" in read("app/templates/lead_forms.html") and "one click = one test" in read("app/templates/lab_board.html")
      and "one click = one board" in read("app/templates/lab.html"))
check("launch counts only subtract blocked accounts still picked",
      "st.excluded.filter(function (x) { return ids.indexOf(x) >= 0; })" in read("app/templates/campaign_launch.html")
      and "st.excluded.filter(function (x) { return ids.indexOf(x) >= 0; })" in read("app/templates/super_launcher.html"))
check("a bad ?bc= / ?state= / ?level= can't kill the page script", read("app/static/accounts.js").count("CSS.escape(") >= 2 and "CSS.escape(preL)" in read("app/templates/monitor.html"))
check("the caption editor cleans up its timer and preview blob on close", "URL.revokeObjectURL(url); url = null;" in read("app/static/creatives.js"))
check("Super Launcher draft survives leaving the page", "keepalive: true" in read("app/templates/super_launcher.html"))
check("launch result polling pauses when hidden and slows down", "if (document.hidden)" in read("app/templates/launch_result.html"))

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
