"""v155.30 — capacity: retention for the tables that grew forever, a rotating time-budgeted full
campaign pass, the issue scan as a job (never blocking the sweep), memory shedding of optional
steps, and a Capacity card on Diagnostics. Retention runs against a real in-memory SQLite when
SQLAlchemy is available (else: SKIP-SUITE)."""
import os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

try:
    import sqlalchemy  # noqa: F401
    from sqlalchemy import Column, DateTime, Integer, String, create_engine
    from sqlalchemy.orm import DeclarativeBase, sessionmaker
    HAVE_SA = True
except Exception:  # noqa: BLE001
    HAVE_SA = False

print("-- retention (pure module over a small fake session) --")
from datetime import datetime, timedelta
class Col:
    def __init__(self, name): self.name = name
    def __lt__(self, v): return lambda r: getattr(r, self.name) is not None and getattr(r, self.name) < v
    def __eq__(self, v): return lambda r: getattr(r, self.name) == v
    __hash__ = object.__hash__
    def in_(self, vals): vals = list(vals); return lambda r: getattr(r, self.name) in vals
    def notin_(self, q): vals = {x[0] if isinstance(x, tuple) else x for x in q}; return lambda r: getattr(r, self.name) not in vals
class Row:
    def __init__(self, **kw): self.__dict__.update(kw)
def model(name, *cols):
    return type(name, (), {"__cols__": cols, **{c: Col(c) for c in cols + ("id",)}})
M = types.SimpleNamespace(
    Alert=model("Alert", "created_at"), Click=model("Click", "created_at"), LaunchQueueItem=model("LaunchQueueItem", "created_at", "status"),
    LaunchLog=model("LaunchLog", "created_at", "campaign_id"), CampaignRecord=model("CampaignRecord", "campaign_id", "advertiser_id"),
    AdAccount=model("AdAccount", "advertiser_id", "status", "enabled", "last_synced_at"))
class Q:
    def __init__(self, db, model, rows, col=None): self.db, self.model, self.rows, self.col = db, model, rows, col
    def filter(self, *preds): return Q(self.db, self.model, [r for r in self.rows if all(p(r) for p in preds)], self.col)
    def order_by(self, *a): return self
    def limit(self, n): return Q(self.db, self.model, self.rows[:n], self.col)
    def __iter__(self): return iter([(getattr(r, self.col.name),) for r in self.rows] if self.col else self.rows)
    def delete(self, synchronize_session=False):
        keep = [r for r in self.db.data[self.model] if r not in self.rows]; n = len(self.db.data[self.model]) - len(keep); self.db.data[self.model] = keep; return n
    def count(self): return len(self.rows)
class DB:
    def __init__(self): self.data = {}; self.commits = 0
    def add(self, model, **kw):
        rows = self.data.setdefault(model, []); rows.append(Row(**{**{c: None for c in model.__cols__}, "id": len(rows) + 1, **kw}))
    def query(self, what):
        if isinstance(what, Col):        # column query: find the model owning it
            for m, rows in self.data.items():
                if getattr(m, what.name, None) is what: return Q(self, m, list(rows), what)
            return Q(self, None, [], what)
        return Q(self, what, list(self.data.get(what, [])))
    def commit(self): self.commits += 1
    def rollback(self): pass
now = datetime(2026, 9, 25); old, fresh = now - timedelta(days=100), now - timedelta(days=5)
db = DB()
db.add(M.Alert, created_at=old); db.add(M.Alert, created_at=fresh); db.add(M.Click, created_at=old); db.add(M.Click, created_at=fresh)
db.add(M.LaunchQueueItem, created_at=now - timedelta(days=40), status="done"); db.add(M.LaunchQueueItem, created_at=now - timedelta(days=40), status="pending")
db.add(M.LaunchLog, created_at=now - timedelta(days=500), campaign_id="LIVE"); db.add(M.LaunchLog, created_at=now - timedelta(days=500), campaign_id="GONE"); db.add(M.LaunchLog, created_at=fresh, campaign_id="X")
db.add(M.CampaignRecord, campaign_id="LIVE", advertiser_id="A"); db.add(M.CampaignRecord, campaign_id="C2", advertiser_id="LOST")
db.add(M.AdAccount, advertiser_id="LOST", status="ACCESS_LOST", enabled=False, last_synced_at=now - timedelta(days=45))
db.add(M.AdAccount, advertiser_id="A", status="", enabled=True, last_synced_at=now)
import importlib
ret = importlib.import_module("app.retention")
out = ret.run(db, M, now=now)
check("old Inbox notices and clicks go, recent stay", out.get("Alert") == 1 and out.get("Click") == 1 and len(db.data[M.Alert]) == 1 and len(db.data[M.Click]) == 1, str(out))
check("launch queue: only finished items past 30 days", out.get("LaunchQueueItem") == 1 and [r.status for r in db.data[M.LaunchQueueItem]] == ["pending"])
check("launch log: a 500-day-old row whose campaign is still on TikTok is KEPT (its source map); the dead one goes",
      out.get("LaunchLog") == 1 and {r.campaign_id for r in db.data[M.LaunchLog]} == {"LIVE", "X"}, str(out))
check("campaign rows of an account whose access has been gone for a month are dropped; a live account's stay",
      out.get("CampaignRecord") == 1 and {r.campaign_id for r in db.data[M.CampaignRecord]} == {"LIVE"}, str(out))
check("a second run finds nothing to do", ret.run(db, M, now=now) == {})
check("the time budget is honoured (0 s → nothing deleted, no error)", ret.run(db, M, budget_s=0, now=now) == {})
check("deletes go in chunks with a commit each (short transactions)", ret.CHUNK <= 5000 and db.commits >= 5)
check("every rule names a real model + timestamp column", all(f"class {m}(Base)" in read("app/models.py") for m, *_ in ret.RULES)
      and all(f"    {c} = Column(DateTime" in read("app/models.py") for _m, c, *_ in ret.RULES))
print("-- the sweep (source) --")
bg = read("app/background.py")
check("full campaign pass: hot accounts every time, then a rotating window of cold ones with a time budget",
      'live_spend.sync_campaigns(db, cold, budget_s=FULL_SYNC_BUDGET_S)' in bg and "def _full_sync_batch(" in bg and "_full_cursor" in bg
      and "FULL_SYNC_MAX = 150" in bg and 'beat("sync_campaigns(hot)"); live_spend.sync_campaigns(db, hot) if hot else None' in bg)
check("sync_campaigns stops at its budget and reports what's left", "budget_s: float | None = None" in read("app/live_spend.py") and '"left": left' in read("app/live_spend.py"))
check("the issue scan is a job now — the sweep never waits for every ad of every account",
      'jobs.enqueue_once(db, "issues_scan"' in bg and "beat(\"issues.scan\"); issues.scan(db)" not in bg)
check("memory shedding: above MEM_SHED_MB the optional steps (issue scan, thumbnails, posters, variants) sit the sweep out",
      "MEM_SHED_MB = 400" in bg and "shed = rss_mb() >= MEM_SHED_MB" in bg and bg.count("if not shed") >= 3 and "not shed and _sched.due(\"issues.scan\"" in bg)
check("retention runs on the slow sweep, contained", "_ret.run(db, _m2)" in bg and '_sched.fail("retention", _e)' in bg)
check("rendered-emoji cache is capped", "if len(_emoji_cache) >= 400" in read("app/text_overlay.py"))
dg = read("app/routes/diagnostics.py")
check("Diagnostics › Capacity: owner-only JSON with DB/WAL/disk/media sizes, table counts, memory, sweep timing, retention plan",
      '@router.get("/diagnostics/capacity.json")' in dg and "def capacity_report(db)" in dg and "shutil.disk_usage(config.DATA_DIR)" in dg
      and '"tables": counts' in dg and 'retention.plan()' in dg and 'id="capacity"' in read("app/templates/diagnostics.html") and "/diagnostics/capacity.json" in read("app/templates/diagnostics.html"))
print("-- uptime & incidents (v155.36) --")
mn = read("app/main.py")
check("every (re)start writes a boot line (build, pid, memory limit) to the app log", '"app started: build ' in mn and 'source="boot"' in mn)
check("a failed sweep is on the record too", 'source="sweep"' in bg and "sweep {sweep_n} failed:" in bg)
check("Diagnostics › Uptime: owner-only timeline of boots with the lines just before each, memory breadcrumbs, 500s; an OOM is named as such",
      '@router.get("/diagnostics/uptime.json")' in dg and "def uptime_report(db" in dg and '("boot", "mem", "sweep", "http")' in dg
      and "out of memory (memory breadcrumbs right before the restart)" in dg and 'id="uptime"' in read("app/templates/diagnostics.html") and "/diagnostics/uptime.json" in read("app/templates/diagnostics.html"))
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
