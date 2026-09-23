"""Phase 2 (v146) — see state without clicking.

  7. Campaign health from ad-group states (Pending tab, explain block, delivering/rejected notices).
  8. Launch record written BEFORE the first create, per-step log, restart recovery, and
     Retry failed that finishes a half-built account inside its own campaign.
  9. TikTok's review verdict kept per launch and shown on posts / creatives / sparks.
 10. Issues keep their start date across rescans; account status changes are dated;
     shared wallets counted once; runway from the 7-day average.

Pure modules run for real (health, launch_trace, review); DB-bound pieces run on light fakes;
routes and templates are source-asserted (they need the full app)."""
import importlib, json, os, subprocess, sys, types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
FUT = "from __future__ import annotations\n"
def grab(src, name):
    i = src.index("def " + name + "(")
    ends = [e for e in (src.find(m, i + 1) for m in ("\ndef ", "\n@", "\nclass ", "\n# ----")) if e != -1]
    return src[i:(min(ends) if ends else len(src))]

# a stand-in for app.database so launch_trace's commits are counted, not run
commits = {"n": 0}
dbmod = types.ModuleType("app.database")
def _safe_commit(db):
    commits["n"] += 1
    db.commit()
    return True
dbmod.safe_commit = _safe_commit
sys.modules["app.database"] = dbmod
health = importlib.import_module("app.health")
lt = importlib.import_module("app.launch_trace")
review = importlib.import_module("app.review")

# =======================================================================================
print("-- 7. campaign health --")
G = lambda sec, op="ENABLE": {"secondary_status": sec, "operation_status": op}
check("all ad groups in review → IN_REVIEW (was 'Active')",
      health.derive("STATUS_ENABLE", [G("ADGROUP_STATUS_AUDIT"), G("ADGROUP_STATUS_AUDIT")])[0] == health.IN_REVIEW)
check("one delivering of three → ACTIVE with the mix",
      health.derive("STATUS_ENABLE", [G("ADGROUP_STATUS_DELIVERY_OK"), G("ADGROUP_STATUS_AUDIT"), G("ADGROUP_STATUS_AUDIT")])
      == (health.ACTIVE, "1 delivering · 2 in review"))
check("suspended account wins over everything",
      health.derive("STATUS_LIMIT", [G("ADGROUP_STATUS_DELIVERY_OK")])[0] == health.ACCOUNT_SUSPENDED)
check("account punish on an ad group → ACCOUNT_SUSPENDED",
      health.derive("STATUS_ENABLE", [G("ADGROUP_STATUS_ADVERTISER_ACCOUNT_PUNISH")])[0] == health.ACCOUNT_SUSPENDED)
check("campaign switched off → PAUSED", health.derive("STATUS_ENABLE", [G("ADGROUP_STATUS_AUDIT")], "DISABLE")[0] == health.PAUSED)
check("rejected", health.derive("", [G("ADGROUP_STATUS_AUDIT_DENY")])[0] == health.REJECTED)
check("balance exceed → NO_FUNDS", health.derive("", [G("ADGROUP_STATUS_BALANCE_EXCEED")])[0] == health.NO_FUNDS)
check("CAMPAIGN_EXCEED without a campaign budget → NO_FUNDS (TikTok's wording for an empty account)",
      health.derive("", [G("ADGROUP_STATUS_CAMPAIGN_EXCEED")])[0] == health.NO_FUNDS)
check("CAMPAIGN_EXCEED with a daily campaign budget → BUDGET_CAPPED",
      health.derive("", [G("ADGROUP_STATUS_CAMPAIGN_EXCEED")], "ENABLE", "BUDGET_MODE_DAY", 50)[0] == health.BUDGET_CAPPED)
check("CREATE reads as scheduled/pending, never 'launch failed'",
      health.derive("", [G("ADGROUP_STATUS_CREATE")])[0] == health.SCHEDULED)
check("unknown status keeps TikTok's raw value", health.derive("", [G("ADGROUP_STATUS_SOMETHING_NEW")]) == (health.UNKNOWN, "ADGROUP_STATUS_SOMETHING_NEW"))
e = health.explain(health.NO_FUNDS, "ADGROUP_STATUS_BALANCE_EXCEED")
check("explain gives title, meaning, fix, bucket, pill", e["title"] == "Out of money" and e["fix"] and e["bucket"] == "blocked" and e["pill"] == "err")
check("in review / scheduled sit in the Pending tab", health.BUCKET[health.IN_REVIEW] == "pending" == health.BUCKET[health.SCHEDULED])
check("transition: review → delivering / rejected; delivering → paused is not news",
      health.transition("ADGROUP_STATUS_AUDIT", "ADGROUP_STATUS_DELIVERY_OK") == "delivering"
      and health.transition("ADGROUP_STATUS_REAUDIT", "ADGROUP_STATUS_AUDIT_DENY") == "rejected"
      and health.transition("ADGROUP_STATUS_DELIVERY_OK", "ADGROUP_STATUS_DISABLE") == "")
st = read("app/routes/status.py")
check("Campaigns page: Pending tab + one bucket rule for rows and counts",
      '"pending"' in st and "def bucket_of" in st and st.count("bucket_of(") >= 3)
check("drawer JSON carries the health explanation", '"health"' in st and "_health_json" in st)
tpl = read("app/templates/status.html")
check("status tabs include Pending", "('pending','Pending')" in tpl.replace(" ", ""))
ags = read("app/adgroup_stats.py")
check("sweep keeps each ad group's state and announces leaving review",
      "def update_states" in ags and "update_states(db, acct, groups)" in ags and 'kind="campaign_resolved"' in ags)
check("…only for tool launches, not warm-ups, and once per campaign/outcome",
      'getattr(lg, "warmup", False)' in ags and "models.Alert.href == href" in ags)
ib = read("app/inbox.py")
check("inbox links a notice to its campaign and titles it", "a.href" in ib and "Launch approved" in ib and "Launch rejected" in ib)

# =======================================================================================
print("-- 8. launch record up front, recovery, resume --")
class Row:
    def __init__(self, **kw):
        self.__dict__.update({"id": None, "status": "running", "campaign_id": "", "campaign_name": "", "inflight": "", "adgroups": "[]",
                              "steps": "[]", "creative_id": None, "spark_code_id": None, "smart_plus": False,
                              "resumed": False, "error": "", "log_id": None, "batch_ref": "", "advertiser_id": "",
                              "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()})
        self.__dict__.update(kw)
class FakeLog:
    def __init__(self, **kw):
        self.__dict__.update({"id": None, "ok": False, "campaign_id": "", "error_code": "", "error_message": "",
                              "error_technical": "", "spark_code_id": None, "batch_ref": "", "advertiser_id": "", "advertiser_name": ""})
        self.__dict__.update(kw)
class FakeAcct:
    def __init__(self, advertiser_id, advertiser_name=""):
        self.advertiser_id, self.advertiser_name = advertiser_id, advertiser_name
class Col:
    def __init__(self, name): self.name = name
    def __eq__(self, v): return ("eq", self.name, v)
    def __ne__(self, v): return ("ne", self.name, v)
    def __lt__(self, v): return ("lt", self.name, v)
    def __hash__(self): return hash(self.name)
class Q:
    def __init__(self, rows): self.rows = list(rows)
    def filter(self, *conds):
        r = self.rows
        for op, name, v in conds:
            r = [x for x in r if {"eq": lambda a: a == v, "ne": lambda a: a != v, "lt": lambda a: a is not None and a < v}[op](getattr(x, name, None))]
        return Q(r)
    def filter_by(self, **kw): return Q([x for x in self.rows if all(getattr(x, k, None) == v for k, v in kw.items())])
    def order_by(self, *a): return self
    def limit(self, n): return Q(self.rows[:n])
    def all(self): return list(self.rows)
    def first(self): return self.rows[0] if self.rows else None
    def delete(self, synchronize_session=False): return 0
class FakeDB:
    def __init__(self): self.tables = {}; self.pending = []; self.n_commit = 0; self._id = 0
    def add(self, o):
        if o not in self.pending: self.pending.append(o)
    def flush(self):
        for o in self.pending:
            if getattr(o, "id", None) is None:
                self._id += 1; o.id = self._id
            lst = self.tables.setdefault(type(o).__name__, [])
            if o not in lst: lst.append(o)
        self.pending = []
    def commit(self): self.flush(); self.n_commit += 1
    def rollback(self): self.pending = []
    def query(self, model): return Q(self.tables.get(model.__name__, []))
class LaunchTrace(Row):
    status = Col("status"); updated_at = Col("updated_at"); created_at = Col("created_at")
LaunchTrace.__name__ = "LaunchTrace"
class LaunchLog(FakeLog): pass
class AdAccount(FakeAcct): pass
M = types.SimpleNamespace(LaunchTrace=LaunchTrace, LaunchLog=LaunchLog, AdAccount=AdAccount)

db = FakeDB()
commits["n"] = 0
t = lt.Trace(db, M, "B1", "111", {"smart_plus": False})
check("the trace row is committed BEFORE anything is sent to TikTok", t.row is not None and commits["n"] == 1 and db.tables["LaunchTrace"])
t.inflight("campaign")
check("a create call is marked in flight (and committed) before it's sent", t.row.inflight == "campaign" and commits["n"] == 2)
t.campaign("C9")
t.inflight("ad group 1"); t.adgroup(0, "AG1")
t.inflight(lt.ad_inflight(0)); t.ad(0, ["AD1"])
t.inflight("ad group 2"); t.adgroup(1, "AG2")
check("campaign id, ad group ids and ad counts are recorded as they come back",
      t.row.campaign_id == "C9" and json.loads(t.row.adgroups) == [{"i": 0, "id": "AG1", "ads": 1, "ad_ids": ["AD1"]}, {"i": 1, "id": "AG2", "ads": 0}])
t.inflight(lt.ad_inflight(1))          # …and the server dies here
check("steps are logged in order", [s["s"] for s in json.loads(t.row.steps)][:3] == ["started", "creating campaign", "campaign created C9"])

# boot recovery
n = lt.recover(db, M, "may or may not have been created")
lg = db.tables["LaunchLog"][0]
check("boot: a running trace becomes 'interrupted' with a failed launch-log row", n == 1 and t.row.status == "interrupted" and lg.error_code == "INTERRUPTED")
check("…that keeps the campaign id, so nothing relaunches a second campaign there", lg.campaign_id == "C9" and t.row.log_id == lg.id)
check("…and says what exists in plain words", "Campaign C9 exists with 2 ad group(s) and 1 ad(s)" in lg.error_message)
check("recover is idempotent", lt.recover(db, M, "x") == 0)

# resume plan from that trace
p = lt.resume_plan(t.row)
check("resume: continue in C9, keep ad group 1 (has an ad) and ad group 2 (its ad may exist unseen)",
      p["campaign_id"] == "C9" and p["done"] == [0, 1] and p["empty_adgroups"] == [])
r2 = Row(campaign_id="C1", adgroups=json.dumps([{"i": 0, "id": "A", "ads": 2}, {"i": 1, "id": "B", "ads": 0}]), inflight="ad group 3")
p2 = lt.resume_plan(r2)
check("resume: an empty ad group is deleted and rebuilt, the finished one kept", p2["done"] == [0] and p2["empty_adgroups"] == ["B"])
t5 = lt.Trace(FakeDB(), M, "B5", "555", {}); t5.campaign("C5", name="Offer_0923_a1")
check("resume keeps the campaign's real name (the ?source= join key)", lt.resume_plan(t5.row)["campaign_name"] == "Offer_0923_a1")
check("no resume for Smart+ (TikTok builds it in one chain) or without a campaign id",
      lt.resume_plan(Row(campaign_id="C", smart_plus=True)) is None and lt.resume_plan(Row(campaign_id="")) is None)
check("ad-in-flight marker never collides with an ad-group marker",
      lt.ad_inflight(0) != "ad group 1" and not "ad group 1".startswith("ad ("))

m1, _ = lt.interrupted_message("", [], "campaign", "may or may not have been created")
m0, _ = lt.interrupted_message("", [], "", "may or may not have been created")
check("interrupted mid-campaign-create → carries the may-have-been-created mark (never auto-relaunched)", "may or may not have been created" in m1)
check("interrupted before any create → 'Safe to retry'", "Safe to retry" in m0)
check("steps are bounded", len(lt.bound_steps([{"t": "", "s": str(i)} for i in range(500)])) == lt.MAX_STEPS)

db2 = FakeDB(); t2 = lt.Trace(db2, M, "B2", "222", {})
t2.campaign("C2"); t2.adgroup(0, "G"); t2.ad(0)
t2.finish(FakeLog(ok=False, campaign_id="C2", error_code="X", error_message="boom"))
check("finish: ads exist but it failed → 'partial'", t2.row.status == "partial")
t3 = lt.Trace(FakeDB(), M, "B3", "333", {}); t3.finish(FakeLog(ok=True, campaign_id="C3"))
check("finish: ok → 'ok'", t3.row.status == "ok")
class Boom:
    def add(self, o): raise RuntimeError("db gone")
t4 = lt.Trace(Boom(), M, "B4", "444", {})
t4.inflight("campaign"); t4.campaign("x"); t4.ad(0); t4.finish(FakeLog())
check("a trace problem never breaks the launch", t4.row is None)

cp = read("app/routes/campaigns.py")
lta = grab(cp, "launch_to_account")
check("launch opens the trace before the try (before any TikTok call)",
      lta.index("_lt.Trace(db, models, batch_ref") < lta.index("    try:\n        # schedule times are read by TikTok in the account's own timezone"))
check("every create is preceded by its in-flight mark",
      lta.index('trace.inflight("campaign")') < lta.index("tiktok_api.create_campaign(")
      and lta.index('trace.inflight(f"ad group {i + 1}")') < lta.index("tiktok_api.create_adgroup(")
      and lta.count("trace.inflight(_lt.ad_inflight(i))") == 2)
check("ad ids are captured from the create answers", "trace.ad(i, _ad_ids(resp))" in lta and "def _ad_ids" in cp)
check("resume: no campaign create, never marked for orphan-delete, finished groups skipped, empties deleted",
      'camp_candidates = []' in lta and "if i in done_groups:" in lta and "update_adgroup_status(" in lta
      and "new_campaign_id = campaign_id     # remember for orphan cleanup on failure" in lta
      and lta.index("if resume and resume.get(\"campaign_id\"):\n                trace.campaign(campaign_id, reused=True") < lta.index("new_campaign_id = campaign_id     # remember"))
check("resume relaunches with the SAME creative / post", 'fields["creative_id"] = int(resume["creative_id"])' in lta and 'fields["spark_code_id"] = int(resume["spark_code_id"])' in lta)
check("the launch log records which creative it used", lta.count("log.creative_id = ") == 2)
check("the trace is finalised with the log and linked after the commit",
      lta.index("trace.finish(log)") < lta.index("db.add(log)\n    db.commit()\n    trace.link(log)"))
rp = grab(cp, "retry_plan")
check("Retry failed: fresh / resume / never-started, skipping accounts something later fixed",
      "relaunch_safe(l)" in rp and "_lt.resume_plan(" in rp and "unstarted" in rp and "relaunched fine since" in rp
      and "a later attempt already continued this campaign" in rp)
check("…no resume for Smart+ / Engaged session, never-started only when the job died (not cancelled)",
      'not fields.get("smart_plus") and not is_engaged(fields)' in rp and '("error", "failed")' in rp)
rf = grab(cp, "retry_failed")
check("retry passes the resume plan through the batch", 'fields["_resume_by_account"] = plan["resume"]' in rf)
check("the resume plan is never saved into a batch recipe", '"_resume_by_account")' in grab(cp, "_remember_batch"))
mn = read("app/main.py")
check("boot recovery runs after jobs.recover, and stuck queue items are closed",
      mn.index("_jobs.recover(_db)") < mn.index("_lt.recover(_db, _m, _mark)") and 'LaunchQueueItem.status == "running"' in mn)
bg = read("app/background.py")
check("the sweep closes traces silent for hours", "launch_trace.recover(db, models, MAYBE_CREATED_MARK, stale=True)" in bg)
check("old traces are pruned", "launch_trace.prune(db, models" in ags)
ta = read("app/tiktok_api.py")
check("ad-group delete/pause is its own idempotent call", "def update_adgroup_status" in ta and '"/adgroup/status/update/"' in ta)
lr = read("app/templates/launch_result.html")
check("result page: steps per account, live 'launching' cards, never-started note, resume hint",
      "tr.steps" in lr and "… launching" in lr and "retry.unstarted" in lr and "l.advertiser_id in retry.resume" in lr)
md = read("app/models.py")
check("LaunchTrace model", "class LaunchTrace(Base):" in md and '__tablename__ = "launch_traces"' in md and "inflight = Column" in md)

# =======================================================================================
print("-- 9. review verdicts --")
check("delivering → approved", review.campaign_verdict([G("ADGROUP_STATUS_DELIVERY_OK"), G("ADGROUP_STATUS_AUDIT_DENY")]) == "approved")
check("every live group denied → rejected (a paused group doesn't count)",
      review.campaign_verdict([G("ADGROUP_STATUS_AUDIT_DENY"), G("ADGROUP_STATUS_AUDIT", "DISABLE")]) == "rejected")
check("budget spent means it was approved", review.campaign_verdict([G("ADGROUP_STATUS_BUDGET_EXCEED")]) == "approved")
check("still in review → pending (not written)", review.campaign_verdict([G("ADGROUP_STATUS_AUDIT")]) == "pending")
now = datetime(2026, 9, 20, 12)
L = lambda **kw: types.SimpleNamespace(**{"ok": True, "campaign_id": "c", "review": "", "created_at": now, **kw})
s = review.summary([L(review="approved", created_at=now - timedelta(days=5)), L(review="rejected", created_at=now - timedelta(days=3)),
                    L(created_at=now - timedelta(hours=2)), L(created_at=now - timedelta(days=30)),
                    L(ok=False, campaign_id="")], now=now)
check("summary counts approved / rejected / in review; an old launch with no verdict is 'unknown', a failed one isn't a test",
      (s["tests"], s["approved"], s["rejected"], s["pending"], s["unknown"]) == (4, 1, 1, 1, 1), s)
check("…and the latest launch's verdict", s["last"] == "pending")
check("label", review.label(s) == "✓ 1 approved · ✕ 1 rejected · 1 in review")
check("post date from the post id (top 32 bits)", review.item_date(str((1726000000 << 32) + 12345)).startswith("2024-09-10"))
check("…and nothing for a non-post id", review.item_date("12345") == "" and review.item_date("abc") == "")
class RQ(Q):
    pass
class LL:
    campaign_id = types.SimpleNamespace(in_=lambda ids: ("in", ids))
logs = [types.SimpleNamespace(campaign_id="c1", review=""), types.SimpleNamespace(campaign_id="c2", review="approved")]
class RDB:
    def query(self, m):
        class _Q:
            def filter(self_, cond):
                return [x for x in logs if x.campaign_id in cond[1]]
        return _Q()
n = review.record(RDB(), types.SimpleNamespace(LaunchLog=LL), {"c1": [G("ADGROUP_STATUS_DELIVERY_OK")], "c2": [G("ADGROUP_STATUS_DELIVERY_OK")], "c3": [G("ADGROUP_STATUS_AUDIT")]})
check("record writes verdicts only where they changed", n == 1 and logs[0].review == "approved" and logs[0].review_at is not None)
check("the sweep records verdicts on every pass", "review.record(db, models, by_c)" in ags)
pv = read("app/profile_videos.py")
ns = {}
exec(FUT + "import importlib\n" + grab(pv, "_created").replace("from .review import item_date", "item_date = importlib.import_module('app.review').item_date"), ns)
check("post create time: unix seconds become a date (used to show '1712345678')",
      ns["_created"]({"create_time": 1726000000}, "1") == "2024-09-10 20:26:40")
check("…missing → read from the post id", ns["_created"]({}, str(1726000000 << 32)).startswith("2024-09-10"))
check("pickers get the history: sparks, creatives, profile posts",
      "review.for_sparks(" in read("app/routes/spark_codes.py") and '"review": rvs.get(r.id)' in read("app/routes/creatives.py")
      and '"reviews": review.for_items(db, models, sc, ids)' in read("app/routes/super_launcher.py"))
check("profile-post history is scoped to the workspace", "sc.owned(q, models.SparkCode)" in read("app/review.py"))
check("creative cards and the Sparks table show the chips",
      "reviews.get(r.id)" in read("app/templates/creatives.html") and "rv.rejected" in read("app/templates/spark_codes.html"))
pk = read("app/static/picker.js")
check("picker renders the chips on creatives, sparks and profile posts",
      pk.count("rvChip(it.review)") == 3 and "rvChip(PV_REVIEWS[v.item_id])" in pk and "PV_REVIEWS[k] = d.reviews[k]" in pk)
js = pk[pk.index("  function rvChip"):pk.index("  UI.rvChip")]
out = subprocess.run(["node", "-e", "var esc=function(s){return String(s)};" + js +
                      "console.log(rvChip({approved:2,rejected:1,pending:0,last:'rejected',last_at:'2026-09-01'}, true));console.log(JSON.stringify(rvChip({})))"],
                     capture_output=True, text=True).stdout
check("rvChip renders ✓/✕ with counts, nothing for an untested post", "✓ 2 approved" in out and "✕ 1 rejected" in out and '""' in out, out)
check("LaunchLog keeps review + creative_id, and the indexes the lookups need",
      'review = Column(String, default="", index=True)' in md and "creative_id = Column(Integer, nullable=True, index=True)" in md
      and 'campaign_id = Column(String, default="", index=True)' in md and 'tiktok_item_id = Column(String, default="", index=True)' in md)
check("indexes on existing tables are built at boot", "index.create(conn, checkfirst=True)" in read("app/database.py"))

# =======================================================================================
print("-- 10. issue dates, shared wallets, runway --")
bal = read("app/balances.py")
ns = {}
for fn in ("shared_wallet", "money_in_bc", "daily_burn", "runway_days"):
    exec(FUT + grab(bal, fn), ns)
check("shared wallet: 20 accounts all reporting the $25 wallet → $25 once, not $500",
      ns["money_in_bc"](25, [25.0] * 20) == {"wallet": 25.0, "in_accounts": 0.0, "total": 25.0, "shared": True})
check("…also when the wallet itself wasn't readable", ns["shared_wallet"](None, [25, 25, 25, 25, 25]) == 25)
check("separate balances are still added up", ns["money_in_bc"](100, [10, 20, 30]) == {"wallet": 100.0, "in_accounts": 60.0, "total": 160.0, "shared": False})
check("two empty accounts aren't a 'shared wallet'", ns["shared_wallet"](0, [0, 0, 0]) is None)
check("a coincidence that doesn't match the wallet isn't one either", ns["shared_wallet"](500, [40, 40, 40]) is None)
check("burn: 7-day average when there's history", ns["daily_burn"](700, 7, 5, 0.1) == 100)
check("burn: no history → today's spend projected over the day (not as a whole day)", ns["daily_burn"](0, 0, 50, 0.25) == 200)
check("runway", ns["runway_days"](1000, 100) == 10 and ns["runway_days"](1000, 0) is None)
mo = read("app/routes/monitor.py")
check("Balances tab uses both", "balances.money_in_bc(" in mo and "balances.daily_burn(" in mo and "balances.burn_by_account(" in mo)
check("…and says 'shared wallet' / the daily rate", "shared wallet" in read("app/templates/monitor.html") and "7-day average" in read("app/templates/monitor.html"))
iss = read("app/issues.py")
ns = {}
exec(FUT + grab(iss, "issue_key") + grab(iss, "issue_since"), ns)
k = ns["issue_key"]
check("same issue across scans despite changing reasons / appeal state / amount",
      k("ad", "1", "9", "secondary_status=AD_STATUS_AUDIT_DENY reasons=x appeal=filed") == k("ad", "1", "9", "secondary_status=AD_STATUS_AUDIT_DENY")
      and k("payment", "1", "", "balance=0.5") == k("payment", "1", "", "balance=0.0"))
check("a different status is a different issue", k("account", "1", "", "status=STATUS_LIMIT") != k("account", "1", "", "status=STATUS_PUNISH"))
week = datetime(2026, 9, 13); nowd = datetime(2026, 9, 20)
check("issue date: earliest of first-seen / status change / TikTok's touch", ns["issue_since"](None, week, None, nowd) == week
      and ns["issue_since"](datetime(2026, 9, 19), None, week, nowd) == week and ns["issue_since"](None, None, None, nowd) == nowd)
check("the scan carries dates over before rebuilding the table",
      iss.index("prior = {issue_key(") < iss.index("db.query(models.Issue).delete()") and "issue._started = _ad_time(ad)" in iss)
check("account status changes are dated by one listener for every writer",
      '@_event.listens_for(AdAccount.status, "set", active_history=True)' in md and "target.status_changed_at = utcnow()" in md)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
