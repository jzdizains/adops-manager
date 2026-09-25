"""Audience sync memory + resume (v131).

The audience refresh job walks 100+ accounts × days × up to nine reports. Before:
every report row became an ORM object via db.add() and, with autoflush off, ALL of an
account-day's rows sat in the session until one commit at the end — a big account was a
~300 MB burst that killed the 512 MB instance at the same minute after every boot, and
the job (never finishing) was re-queued from account #1 on every restart: a crash loop.

This suite proves:
  · _store writes plain dicts in batches of STORE_BATCH through db.execute (executemany),
    never db.add — the FakeDB has NO .add, so any use of it fails loudly
  · one commit per report (the writer is released before the next network call)
  · duplicates and rows without a campaign id are skipped; hour keys parse
  · the "no delivery" short-circuit keys off what was STORED (age × gender empty → skip)
  · sync() checkpoints per account: a run killed mid-way resumes after the accounts
    already done (same days), and clears the checkpoint when it finishes
fastapi/sqlalchemy stubbed (not installed here)."""
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

class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
    def __getattr__(self, n): return _Any()

# ---- stubs -----------------------------------------------------------------------
class InsertStmt:
    def __init__(self, table): self.table = table
sa = _mod("sqlalchemy", func=_Any(), insert=lambda t: InsertStmt(t))
_mod("sqlalchemy.orm", Session=_Any); sa.orm = sys.modules["sqlalchemy.orm"]
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]

class AudienceStat: pass
class RegionName: id = _Any()
_mod("app.models", AudienceStat=AudienceStat, RegionName=RegionName, AdAccount=_Any, CampaignRecord=_Any)

SETTINGS: dict[str, str] = {}
def get_setting(db, key, default=""): return SETTINGS.get(key, default)
def set_setting(db, key, value): SETTINGS[key] = value; db.commit()
_mod("app.queries", get_setting=get_setting, set_setting=set_setting, enabled_accounts=lambda db: [])
class TikTokError(Exception): pass
_mod("app.tiktok_api", TikTokError=TikTokError, get_audience_report=lambda *a, **k: [], get_hourly_report=lambda *a, **k: [])
_mod("app.timeutil", local_date_str=lambda: "2026-09-20", now_utc=lambda: None)

import importlib
audience = importlib.import_module("app.audience")

class Q:
    def __init__(self, db): self.db = db
    def filter_by(self, **kw): self.db.deletes.append(kw); return self
    def delete(self, synchronize_session=None): return 0
    def scalar(self): return 1          # region names exist → the weekly name refresh is skipped

class FakeDB:
    """No .add on purpose: the memory fix must never build per-row ORM objects."""
    def __init__(self):
        self.batches: list[list[dict]] = []; self.deletes = []; self.commits = 0; self.rollbacks = 0
    def query(self, *a, **k): return Q(self)
    def execute(self, stmt, params=None):
        assert isinstance(stmt, InsertStmt) and stmt.table is AudienceStat, "must be an INSERT into AudienceStat"
        assert isinstance(params, list) and params and isinstance(params[0], dict), "executemany with a list of dicts"
        self.batches.append(list(params))
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1

class Acct:
    def __init__(self, i): self.advertiser_id = f"adv{i}"; self.access_token = "t"; self.advertiser_name = f"Account {i}"

print("-- _store: bounded batches of plain dicts, one commit --")
rows = []
for i in range(1200):
    rows.append({"dimensions": {"campaign_id": f"c{i % 1150}", "age": "AGE_25_34", "gender": "MALE"},
                 "metrics": {"spend": "1.5", "impressions": "10", "clicks": "2", "conversion": "1", "reach": "9", "campaign_name": "Camp"}})
rows.append({"dimensions": {"age": "AGE_18_24", "gender": "FEMALE"}, "metrics": {}})          # no campaign id → skipped
db = FakeDB()
n = audience._store(db, Acct(1), "2026-09-19", "age_gender", rows, audience.DIMS["age_gender"])
sizes = [len(b) for b in db.batches]
check("1200 rows with 50 dupes → 1150 stored, in batches of ≤500", n == 1150 and sizes == [500, 500, 150], (n, sizes))
check("exactly ONE commit per report (writer released before the next network call)", db.commits == 1, db.commits)
check("old rows for (account, day, dim) deleted first", db.deletes and db.deletes[0] == {"advertiser_id": "adv1", "date": "2026-09-19", "dim": "age_gender"}, db.deletes)
row0 = db.batches[0][0]
check("row is a plain dict with the model's columns, key = age|gender", row0["key"] == "AGE_25_34|MALE" and row0["spend"] == 1.5 and row0["reach"] == 9 and row0["dim"] == "age_gender" and "synced_at" in row0, row0)
check("STORE_BATCH is small (peak memory = one batch, not an account-day)", 100 <= audience.STORE_BATCH <= 1000, audience.STORE_BATCH)

db2 = FakeDB()
hrows = [{"dimensions": {"campaign_id": "c1", "stat_time_hour": "2026-09-20 14:00:00"}, "metrics": {"spend": "2"}},
         {"dimensions": {"campaign_id": "c1", "stat_time_hour": "bad"}, "metrics": {}}]
n2 = audience._store(db2, Acct(2), "2026-09-20", audience.HOUR, hrows)
check("hour rows: key is the hour number; unparsable stamp skipped", n2 == 1 and db2.batches[0][0]["key"] == "14", (n2, db2.batches))

print("-- streaming: a report is consumed page by page (peak = one page) --")
# _Sink fed in pages must dedup ACROSS pages and hold only the current batch
db3 = FakeDB()
sink = audience._Sink(db3, Acct(3), "2026-09-19", "age_gender", audience.DIMS["age_gender"])
page = [{"dimensions": {"campaign_id": "c1", "age": "AGE_25_34", "gender": "MALE"}, "metrics": {"spend": "1"}}]
sink.add(page); sink.add(page)     # same row on two pages → stored once (cross-page dedup)
sink.add([{"dimensions": {"campaign_id": "c2", "age": "AGE_18_24", "gender": "FEMALE"}, "metrics": {"spend": "2"}}])
total = sink.finish()
check("cross-page dedup; delete once up front; one commit at finish", total == 2 and db3.deletes[0]["dim"] == "age_gender" and db3.commits == 1, (total, db3.commits))
big = [{"dimensions": {"campaign_id": f"c{i}", "age": "AGE_25_34", "gender": "MALE"}, "metrics": {}} for i in range(600)]
db4 = FakeDB(); s4 = audience._Sink(db4, Acct(4), "2026-09-19", "age_gender", audience.DIMS["age_gender"])
s4.add(big); mid = len(db4.batches)     # a 600-row page already flushed one 500 batch before finish
s4.finish()
check("a page flushes at STORE_BATCH, so RAM never holds more than one batch", mid == 1 and [len(b) for b in db4.batches] == [500, 100], [len(b) for b in db4.batches])

src = open(os.path.join(ROOT, "app", "audience.py"), encoding="utf-8").read()
sink_src = src.split("class _Sink")[1].split("\ndef _store(")[0]
tk = open(os.path.join(ROOT, "app", "tiktok_api.py"), encoding="utf-8").read()
check("source: the sink never uses db.add (no per-row ORM objects)", "db.add(" not in sink_src and "self.db.execute(self.stmt" in sink_src)
check("source: reports stream into the sink via on_page (no full report in RAM)",
      "on_page=sink.add" in src and "sink.finish()" in src and "def get_report_pages(" in tk and "if on_page is not None:" in tk and "on_page(rows)" in tk)
check("source: the no-delivery short-circuit keys off the STORED count", 'stored = sink.finish()' in src and "if not stored:" in src)
check("source: audience job records its own peak RSS (it runs in the jobs worker, not the sweep)", '"peak_mb"' in src and 'source="mem"' in src)
bg = open(os.path.join(ROOT, "app", "background.py"), encoding="utf-8").read()
check("source: the sweep logs a 'mem trail' breadcrumb naming the step BEFORE it runs when RSS is high", "peak[\"mb\"], peak[\"step\"]" in bg and 'source="mem"' in bg and "MEM_TRAIL_MB" in bg and 'jobs.enqueue_once(db, "issues_scan"' in bg)   # v155.30: the scan runs as a job

print("-- sync(): checkpoint per account, resume after a restart --")
class Killed(BaseException):
    """Simulates the process dying (OOM / deploy) — not an Exception, nothing catches it."""

calls: list[str] = []
def fake_day(db, acct, day, *, hours, audience, should_stop=None):
    calls.append(acct.advertiser_id + ":" + day)
    if acct.advertiser_id == "adv3" and fake_day.kill_first:
        fake_day.kill_first = False
        raise Killed()
    return {"rows": 5, "calls": 1, "skipped": False, "error": ""}
fake_day.kill_first = True
audience.sync_account_day = fake_day
audience.hot_accounts = lambda db: [Acct(i) for i in range(1, 6)]
audience.refresh_region_names = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run on a hot-only pass with names present"))
audience.prune = lambda *a, **k: (_ for _ in ()).throw(AssertionError("prune must not run on a hot-only pass"))
SETTINGS.clear()
days = {"hours": ["2026-09-20"], "audience": ["2026-09-20", "2026-09-19"]}

db3 = FakeDB(); killed = False
try:
    audience.sync(db3, days, hot_only=True)
except Killed:
    killed = True
ck = json.loads(SETTINGS.get(audience.CHECKPOINT_KEY) or "{}")
check("run 1 died at account 3; checkpoint holds the 2 accounts already done", killed and sorted(ck.get("done") or []) == ["adv1", "adv2"], ck)
calls_before = list(calls); calls.clear()

db4 = FakeDB()
r = audience.sync(db4, days, hot_only=True)
touched = sorted({c.split(":")[0] for c in calls})
check("run 2 (same days) resumes: accounts 1–2 skipped, 3–5 synced", touched == ["adv3", "adv4", "adv5"] and r["resumed"] == 2 and r["ok"] == 5 and r["failed"] == 0, (touched, r))
check("checkpoint cleared when the run finishes; synced_at stamps written", SETTINGS.get(audience.CHECKPOINT_KEY) == "" and SETTINGS.get("audience_breakdown_synced_at") and SETTINGS.get("audience_hours_synced_at"), {k: v[:20] for k, v in SETTINGS.items()})

calls.clear()
r2 = audience.sync(db4, {"hours": ["2026-09-21"], "audience": ["2026-09-21"]}, hot_only=True)
check("different days → a fresh run over every account (no stale resume)", r2["resumed"] == 0 and len({c.split(':')[0] for c in calls}) == 5, (r2, len(calls)))

print("-- job card says when it resumed --")
jh = open(os.path.join(ROOT, "app", "job_handlers.py"), encoding="utf-8").read()
check("audience_sync handler mentions the resume in its result line", "already done before a restart" in jh)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
