"""Super Launcher › the creative board (v123): library creatives, spark codes and profile
posts picked together onto one board, spread over the accounts in the picked order, and
autosave/resume of the launcher.

Functional (fastapi/sqlalchemy stubbed, a tiny in-memory db):
- parse_items: only readable picks, kinds library|spark|profile, capped
- creative_units: only what the workspace owns; processing/broken library rows skipped;
  profile posts become their spark rows; duplicates collapse; picked order kept
- the spread maths the launch route uses (0 = evenly over every account, N = cap)
- job handler: a batch with BOTH pairs and spark_pairs runs both under one batch_ref and
  remembers a merged recipe; progress counts across both
- retry: _creative_by_account + _spark_by_account → pairs + spark_pairs
- draft: key per logged-in user; oversized / unreadable state refused
Static: template (board, add menu, autosave chip, resume slot), picker.js pop-ups,
JSON answers of /creatives/upload and /spark-codes/bulk, CSS."""
import os, sys, types, json

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
_mod("fastapi.responses", JSONResponse=_Any, RedirectResponse=_Any)
sa = _mod("sqlalchemy", func=_Any()); _mod("sqlalchemy.orm", Session=_Any); sa.orm = sys.modules["sqlalchemy.orm"]
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]

# ---- tiny db ---------------------------------------------------------------------
class Creative:
    def __init__(self, id, owner, status="available", error="", kind="video"):
        self.id, self.owner_user_id, self.status, self.error, self.kind = id, owner, status, error, kind
class SparkCode:
    def __init__(self, id, owner, tiktok_item_id=None):
        self.id, self.owner_user_id, self.tiktok_item_id = id, owner, tiktok_item_id
class Col:
    def __init__(self, name): self.name = name
    def in_(self, vals): vals = set(vals); return lambda r: getattr(r, self.name) in vals
class Model:
    def __init__(self, cls): self.cls = cls; self.id = Col("id"); self.owner_user_id = Col("owner_user_id")
models = _mod("app.models", Creative=Model(Creative), SparkCode=Model(SparkCode), AdAccount=_Any, Template=_Any, Job=_Any,
              CampaignRecord=_Any, LaunchLog=_Any, BusinessCenter=_Any)
ROWS = {Creative: [Creative(1, 7), Creative(2, 7, kind="carousel"), Creative(3, 7, status="processing"), Creative(4, 7, error="broken"), Creative(9, 8)],
        SparkCode: [SparkCode(10, 7), SparkCode(11, 8), SparkCode(12, 7, tiktok_item_id="700001")]}
class Q:
    def __init__(self, rows): self.rows = list(rows)
    def filter(self, *conds):
        out = self.rows
        for c in conds: out = [r for r in out if c(r)]
        return Q(out)
    def __iter__(self): return iter(self.rows)
class DB:
    def query(self, model): return Q(ROWS[model.cls])
class Scope:
    def __init__(self, uid): self.user_id = uid; self.me_id = uid
    def owned(self, q, model): return q.filter(lambda r: r.owner_user_id == self.user_id)
# profile posts → spark rows (the real ensure_spark_rows is covered by profile_videos_test)
made = []
def ensure_spark_rows(db, sc, items):
    out = []
    for it in items:
        row = [r for r in ROWS[SparkCode] if r.tiktok_item_id == str(it["item_id"])]
        if not row:
            row = [SparkCode(100 + len(made), sc.user_id, str(it["item_id"]))]; ROWS[SparkCode].append(row[0]); made.append(row[0])
        out.append(row[0])
    return out
_mod("app.profile_videos", ensure_spark_rows=ensure_spark_rows, assign=lambda a, s, p: [])
_mod("app.database", get_db=lambda: None)
_mod("app.templating", render=lambda *a, **k: None)
_mod("app.routes.campaigns", queue_launch=lambda *a, **k: "ref", assign_creatives=lambda *a: [], error_messages=_Any())
_mod("app.routes.launch", synthesize=lambda *a, **k: {}, destination_label=lambda f: "", OBJECTIVE_OPTIONS=[])
SETTINGS = {}
_mod("app.queries", get_setting=lambda db, k, d="": SETTINGS.get(k, d), set_setting=lambda db, k, v: SETTINGS.__setitem__(k, v))

import importlib
sl = importlib.import_module("app.routes.super_launcher")

print("-- parse_items --")
raw = json.dumps([{"kind": "library", "id": "1"}, {"kind": "spark", "id": 10}, {"kind": "profile", "item_id": "700001", "handle": "x"},
                  {"kind": "library", "id": "abc"}, {"kind": "nope", "id": 1}, "junk", {"kind": "profile", "item_id": ""}])
items = sl.parse_items(raw)
check("readable picks only, kinds kept", [(i["kind"], i.get("id") or i.get("item_id")) for i in items] == [("library", 1), ("spark", 10), ("profile", "700001")], items)
check("unreadable → empty", sl.parse_items("{not json") == [] and sl.parse_items(json.dumps({"a": 1})) == [] and sl.parse_items("") == [])
check("capped at ITEMS_MAX", len(sl.parse_items(json.dumps([{"kind": "library", "id": i} for i in range(500)]))) == sl.ITEMS_MAX)

print("-- creative_units --")
db, sc = DB(), Scope(7)
units, why = sl.creative_units(db, sc, json.dumps([
    {"kind": "spark", "id": 10}, {"kind": "library", "id": 2}, {"kind": "profile", "item_id": "700001"}, {"kind": "library", "id": 1},
    {"kind": "library", "id": 9},          # another workspace's
    {"kind": "spark", "id": 11},           # another workspace's
    {"kind": "library", "id": 3},          # still processing
    {"kind": "library", "id": 4},          # broken
    {"kind": "profile", "item_id": "700002", "handle": "new.one"},   # no row yet → made
    {"kind": "spark", "id": 10},           # duplicate
]))
check("picked order kept; only owned, ready rows; profile → its spark row; dupes collapse",
      units == [("spark", 10), ("library", 2), ("spark", 12), ("library", 1), ("spark", made[0].id)] and not why, (units, why))
check("a post without a row gets one, in this workspace", made and made[0].owner_user_id == 7 and made[0].tiktok_item_id == "700002")
check("nothing usable → a plain-words problem", sl.creative_units(db, sc, json.dumps([{"kind": "library", "id": 9}]))[1].startswith("None of the picked creatives"))
check("nothing picked → asks for one", sl.creative_units(db, sc, "[]") == ([], "Pick at least one creative."))

print("-- spread maths (as the launch route does it) --")
def spread(n_accounts, units, per):
    accounts = list(range(n_accounts))
    if per <= 0:
        per = max(1, -(-len(accounts) // len(units)))
    else:
        accounts = accounts[:len(units) * per]
    return [(a, units[i // per]) for i, a in enumerate(accounts)]
u3 = [("spark", 1), ("library", 2), ("spark", 3)]
check("0 = evenly over every account: 10 accounts / 3 picks → 4,4,2; every account served", [x[1] for x in spread(10, u3, 0)] == [u3[0]] * 4 + [u3[1]] * 4 + [u3[2]] * 2)
check("one pick, 0 → all accounts get it (the old spark mode)", [x[1] for x in spread(100, [("spark", 1)], 0)] == [("spark", 1)] * 100)
check("N caps the accounts: 3 picks × 2 → 6 of 10 accounts", len(spread(10, u3, 2)) == 6 and [x[1] for x in spread(10, u3, 2)] == [u3[0]] * 2 + [u3[1]] * 2 + [u3[2]] * 2)
check("more picks than accounts: the extra picks are simply not used", [x[1] for x in spread(2, u3, 0)] == [u3[0], u3[1]])
src = open(os.path.join(ROOT, "app/routes/super_launcher.py")).read()
check("route: items mode → creative_units → pairs + spark_pairs in ONE queue_launch, draft cleared",
      'if creative_mode == "items":' in src and "units, why = creative_units(db, sc, form.get(\"items\"))" in src
      and 'lib_pairs = [[a.advertiser_id, ref_id] for a, (k, ref_id) in assigned if k == "library"]' in src
      and "pairs=lib_pairs or None, spark_pairs=spk_pairs or None)" in src and src.count("clear_draft(db, sc)") >= 4
      and 'per = max(1, -(-len(accounts) // len(units)))' in src)
check("route: all-spark boards run as spark with fixed text; a mixed board leaves the preset's text mode for library creatives",
      'if all(k == "spark" for k, _ in units):\n            fields["creative_source"] = "spark"\n            fields["ad_text_mode"] = "fixed"' in src)
check("route: the board's picks never use the retry queue path (assignment needs a fixed list)", "or bool(profile_sparks) or bool(units)" in src)

print("-- draft --")
SETTINGS.clear()
check("key is per logged-in user", sl.draft_key(Scope(7)) == "launch_draft:u7" and sl.draft_key(Scope(3)) == "launch_draft:u3")
sl.clear_draft(db, sc); check("clear writes an empty value", SETTINGS.get("launch_draft:u7") == "")
check("size cap is generous but bounded (≤ 96 KB)", sl.DRAFT_MAX == 96 * 1024)
check("draft routes exist (GET json, POST save/clear)", '@router.get("/super-launcher/draft.json")' in src and '@router.post("/super-launcher/draft")' in src and 'if form.get("clear"):' in src and "status_code=413" in src)

print("-- job handler: mixed batch --")
calls = []
class FakeEngine:
    class error_messages:
        @staticmethod
        def new_ref(): return "newref"
    @staticmethod
    def run_batch_assigned(db, pairs, fields, batch_ref=None, on_progress=None):
        calls.append(("lib", [(a.advertiser_id, c) for a, c in pairs], batch_ref))
        for i in range(len(pairs)): on_progress(i + 1, len(pairs))
        return batch_ref
    @staticmethod
    def run_batch_assigned_sparks(db, pairs, fields, batch_ref=None, on_progress=None):
        calls.append(("spark", [(a.advertiser_id, s) for a, s in pairs], batch_ref))
        for i in range(len(pairs)): on_progress(i + 1, len(pairs))
        return batch_ref
    @staticmethod
    def run_batch(db, accounts, fields, batch_ref=None, on_progress=None):
        calls.append(("plain", [a.advertiser_id for a in accounts], batch_ref)); return batch_ref
    remembered = {}
    @staticmethod
    def _remember_batch(db, ref, fields): FakeEngine.remembered[ref] = fields
sys.modules["app.routes.campaigns"] = FakeEngine
class Acct:
    def __init__(self, i): self.advertiser_id = str(i)
class Log:
    def __init__(self, ok): self.ok = ok
class JDB:
    def query(self, model):
        class R:
            def filter(self, *a): return self
            def filter_by(self, **k): return self
            def all(self): return [Log(True), Log(True), Log(False)]
        class A:
            def filter(self, cond): return self
            def all(self): return [Acct(1), Acct(2), Acct(3), Acct(4)]
        return A() if model is models.AdAccount else R()
progress = []
_mod("app.jobs", handler=lambda kind: (lambda f: f), progress=lambda db, job, text: progress.append(text), enqueue=lambda *a, **k: None, should_stop=lambda *a: False)
_mod("app.queries", get_setting=lambda *a: "", set_setting=lambda *a: None)
jh = importlib.import_module("app.job_handlers")
models.AdAccount = type("AdAccount", (), {"advertiser_id": Col("advertiser_id")})
out = jh._launch(JDB(), {"batch_ref": "b1", "advertiser_ids": ["1", "2", "3", "4"], "fields": {"x": 1},
                         "pairs": [["1", 2], ["2", 1]], "spark_pairs": [["3", 10], ["4", 12]]}, None)
check("both runners ran, under the same batch_ref, library first", [c[0] for c in calls] == ["lib", "spark"] and calls[0][2] == "b1" and calls[1][2] == "b1"
      and calls[0][1] == [("1", 2), ("2", 1)] and calls[1][1] == [("3", 10), ("4", 12)], calls)
check("one progress count across both", progress == ["1 of 4", "2 of 4", "3 of 4", "4 of 4"], progress)
check("merged recipe: both per-account maps kept", FakeEngine.remembered.get("b1") == {"x": 1, "_creative_by_account": {"1": 2, "2": 1}, "_spark_by_account": {"3": 10, "4": 12}}, FakeEngine.remembered)
check("result counted from the batch's logs", out["href"] == "/campaigns/result/b1" and "2 launched, 1 failed" in out["detail"], out)
calls.clear(); progress.clear()
jh._launch(JDB(), {"batch_ref": "b2", "advertiser_ids": ["1"], "fields": {}, "pairs": None, "spark_pairs": [["1", 10]]}, None)
check("spark-only batch: only the spark runner, no merged rewrite", [c[0] for c in calls] == ["spark"] and "b2" not in FakeEngine.remembered)
calls.clear()
jh._launch(JDB(), {"batch_ref": "b3", "advertiser_ids": ["1", "2"], "fields": {}}, None)
check("no pairs at all → the plain runner", [c[0] for c in calls] == ["plain"])

print("-- retry keeps the assignment --")
ca = open(os.path.join(ROOT, "app/routes/campaigns.py")).read()
seg = ca.split("def retry_failed")[1][:3000]
check("retry reads both maps and queues pairs + spark_pairs", 'by_creative = fields.pop("_creative_by_account", None) or {}' in seg and "pairs=lib_pairs or None, spark_pairs=pairs or None)" in seg and "a.advertiser_id not in by_acct]" in seg)
check("the library runner remembers which creative each account got", '"_creative_by_account": {str(a.advertiser_id): int(cid) for a, cid in pairs if cid is not None}' in ca.split("def run_batch_assigned(")[1][:1200])

print("-- static: page, pickers, routes, CSS --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
th = read("app/templates/super_launcher.html"); pj = read("app/static/picker.js"); css = read("app/static/style.css")
check("creative step: one 'Choose myself…' mode, the board, the fresh-items strip, per-creative 0 = spread",
      'data-mode="items"' in th and 'id="slBoard"' in th and 'id="slBoardGrid"' in th and 'id="slNext"' in th and 'name="accounts_per_creative" min="0" value="0"' in th
      and 'data-mode="pick"' not in th and 'data-mode="spark"' not in th and 'data-mode="profile"' not in th and 'name="spark_code_id"' not in th)
check("board: tiles with cover, source badge, type, order number, remove; click = preview; add menu with five sources",
      'class="cb-tile"' in th and 'class="cb-src' in th and 'class="cb-type"' in th and 'class="cb-no"' in th and 'class="cb-x"' in th and "UI.previewCreative({" in th
      and all(('data-src="%s"' % s) in th for s in ("library", "profile", "spark", "upload", "paste")) and "UI.uploadCreatives()" in th and "UI.pasteSparks()" in th and "UI.pickSpark({ multi: true" in th)
check("board picks are serialised to the hidden items field (library/spark by id, profile with its identity)",
      'return { kind: it.kind, id: it.id };' in th and 'item_id: it.item_id, identity_id: it.identity_id' in th)
check("review + confirm show the creatives as a strip", 'id="slReviewStrip"' in th and "stripHtml(items, 12)" in th)
check("autosave: chip, debounced save, resume banner from the server draft, cleared on discard, not offered when arriving with picks",
      'id="slSaved"' in th and 'id="slResume"' in th and 'UI.post("/super-launcher/draft", { state: json })' in th and 'UI.get("/super-launcher/draft.json")' in th
      and 'UI.post("/super-launcher/draft", { clear: "1" })' in th and "if (!arrivedWith)" in th and "saveTimer = setTimeout(saveNow, 1500)" in th)
check("resume restores preset, board, accounts, auto-pick, duplication, queue flag and the step", "function apply(st)" in th and "picker.selectRows(function (row)" in th and 'wiz.show(st.step || "preset", true)' in th)
check("error flash shows the real message", "{{ request.query_params.get('err') }}" in th)
check("picker.js: multi spark picker, upload pop-up with progress, paste-codes pop-up, preview pop-up",
      "function pickSparks(o)" in pj and "if (o.multi) return pickSparks(o);" in pj and "UI.uploadCreatives = function" in pj and "xhr.upload.onprogress" in pj
      and "UI.pasteSparks = function" in pj and 'UI.post("/spark-codes/bulk"' in pj and "UI.previewCreative = function" in pj and "preview: v.preview || \"\"" in pj)
cr = read("app/routes/creatives.py"); sp = read("app/routes/spark_codes.py")
check("/creatives/upload answers JSON with the new rows to fetch callers", "if _wants_json(request):" in cr.split("async def upload_creatives")[1] and "new_rows.append(row)" in cr and '"poster": f"/creatives/{r.id}/poster"' in cr)
check("/spark-codes/bulk answers JSON with the added (and already-known) codes, picker-shaped", "def pick_item(s, rv: dict | None = None) -> dict:" in sp and '"items": [pick_item(r) for r in touched]' in sp and "touched.append(had)" in sp)
check("CSS: board, menu, strips, upload, chip", all(k in css for k in (".cb-tile", ".cb-add", ".cb-menu", ".cb-strip", ".cb-next", ".up-drop", ".sl-saved", ".sl-resume")))
check("STATIC_VERSION bumped", 'STATIC_VERSION = "159"' in read("app/config.py"))
check("Single campaign page keeps its single spark picker", "UI.pickSpark({ selected:" in read("app/templates/campaign_launch.html") or "UI.pickSpark(" in read("app/templates/campaign_launch.html"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
