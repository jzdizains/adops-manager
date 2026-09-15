"""Job lanes for several users at once (v116).

- lane(): launches → "launch", the manual campaign sync → "sync", long sweeps → "slow",
  everything else → "fast".
- the launch lane runs LAUNCH_WORKERS threads; a job is claimed atomically so two workers
  never run the same launch.
- fairness: a user with a launch already running yields to a user without one; within a
  user, oldest first; when everyone queued is busy, oldest first.
- the retry queue takes one item per user per round.
Runs without sqlalchemy (stubbed)."""
import json, os, sys, threading, types

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
_mod("sqlalchemy.orm", Session=object)
class Cond(tuple):
    def __invert__(self): return Cond(("not", self))
class Col:
    def __init__(self, n): self.n = n
    def __eq__(self, o): return Cond(("eq", self.n, o))
    def in_(self, v): return Cond(("in", self.n, set(v)))
    __hash__ = object.__hash__
class Job:
    id = Col("id"); status = Col("status"); kind = Col("kind")
    _seq = 0
    def __init__(self, kind, payload=None, status="queued"):
        Job._seq += 1
        self.id, self.kind, self.status = Job._seq, kind, status
        self.payload = json.dumps(payload or {}); self.title = kind
        self.started_at = self.finished_at = None; self.detail = ""; self.progress = ""; self.href = ""; self.quiet = False; self.seen = False
        self.cancel_requested = False
models = _mod("app.models", Job=Job)

def _match(row, cond):
    if isinstance(cond, tuple) and cond[0] == "not":
        return not _match(row, cond[1])
    kind, col, val = cond
    v = getattr(row, col)
    return v == val if kind == "eq" else v in val
_UPDATE_LOCK = threading.Lock()
class Q:
    def __init__(self, rows, conds=()): self.rows = list(rows); self.conds = tuple(conds)
    def filter(self, *conds): return Q([r for r in self.rows if all(_match(r, c) for c in conds)], self.conds + conds)
    def order_by(self, *a): return Q(sorted(self.rows, key=lambda r: r.id))
    def limit(self, n): return Q(self.rows[:n])
    def all(self): return list(self.rows)
    def first(self): return self.rows[0] if self.rows else None
    def update(self, values, synchronize_session=False):
        # like the real UPDATE … WHERE status='queued': one writer at a time, re-checked under the lock
        with _UPDATE_LOCK:
            live = [r for r in self.rows if all(_match(r, c) for c in self.conds)]
            for r in live:
                for k, v in values.items(): setattr(r, k.n, v)
            return len(live)
class DB:
    def __init__(self, jobs): self.jobs = jobs; self.lock = threading.Lock()
    def query(self, model): return Q(self.jobs)
    def commit(self): pass
    def rollback(self): pass
    def refresh(self, j): pass
    def merge(self, j): return j

import importlib
jobs = importlib.import_module("app.jobs")

print("\n-- lanes --")
check("launch → its own lane", jobs.lane("launch") == "launch")
check("manual campaign sync → its own lane (never in front of a launch, never behind a scan)", jobs.lane("status_sync") == "sync")
check("long sweeps → slow", jobs.lane("issues_scan") == "slow" and jobs.lane("adgroup_duplicate") == "slow")
check("bids / edits → fast", jobs.lane("bid") == "fast" and jobs.lane("campaign_edit") == "fast")
check("four lanes, ≥2 launch workers", jobs.LANES == ("fast", "slow", "launch", "sync") and jobs.LAUNCH_WORKERS >= 2)

print("\n-- fairness --")
def L(user): return Job("launch", {"fields": {"_launched_by": user}})
a1, a2, a3, b1 = L(1), L(1), L(1), L(2)          # user 1 queued three, user 2 one, later
db = DB([a1, a2, a3, b1])
jobs._running_launch_users.clear()
check("nothing running → oldest first (user 1's first)", jobs._next_launch(db) is a1)
jobs._running_launch_users[a1.id] = "1"; a1.status = "running"
check("user 1 has one running → user 2's launch goes next even though it's newer", jobs._next_launch(db) is b1)
jobs._running_launch_users[b1.id] = "2"; b1.status = "running"
check("everyone queued is busy → oldest queued (user 1's second)", jobs._next_launch(db) is a2)
jobs._running_launch_users.clear()
check("no queued launch → None", jobs._next_launch(DB([Job("bid")])) is None)

print("\n-- atomic claim --")
j = Job("launch", {"fields": {"_launched_by": 1}})
db = DB([j])
check("first claim wins", jobs._claim(db, j.id) and j.status == "claimed")
check("second claim loses", not jobs._claim(db, j.id))
ran = []
jobs.HANDLERS["launch"] = lambda db, p, job: ran.append(job.id) or {"ok": True}
j2, j3 = L(1), L(2)
db = DB([j2, j3])
jobs._running_launch_users.clear()
n = jobs.run_pending(db, which="launch")
check("run_pending(launch) runs every queued launch once, marks them done", n == 2 and sorted(ran) == [j2.id, j3.id] and j2.status == "done" and j3.status == "done" and not jobs._running_launch_users)
ran.clear()
j4 = L(1); k = Job("status_sync"); s = Job("issues_scan"); f = Job("bid")
jobs.HANDLERS["launch"] = jobs.HANDLERS["status_sync"] = jobs.HANDLERS["issues_scan"] = jobs.HANDLERS["bid"] = lambda db, p, job: ran.append(job.kind) or {"ok": True}
db = DB([j4, k, s, f])
jobs.run_pending(db, which="fast")
check("the fast lane runs only fast jobs (no launch, no sync, no scan)", ran == ["bid"])
ran.clear(); jobs.run_pending(db, which="sync")
check("the sync lane runs the campaign sync only", ran == ["status_sync"])
ran.clear(); jobs.run_pending(db, which="slow")
check("the slow lane runs the scan only", ran == ["issues_scan"])
ran.clear(); jobs.run_pending(db, which="launch")
check("the launch lane runs the launch", ran == ["launch"])
# two workers racing on one launch: only one runs it
ran.clear(); race = L(3); db = DB([race])
def worker():
    jobs.run_pending(db, which="launch")
ts = [threading.Thread(target=worker) for _ in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
check("four workers, one launch → it runs exactly once", ran == ["launch"], str(ran))

print("\n-- retry queue --")
qw = open(os.path.join(ROOT, "app", "queue_worker.py"), encoding="utf-8").read()
check("retry queue takes one item per user per round", "by_user.setdefault(it.launched_by, []).append(it)" in qw and "while len(items) < limit and any(by_user.values()):" in qw)
src = open(os.path.join(ROOT, "app", "jobs.py"), encoding="utf-8").read()
check("boot recovery and 'pending' know the claimed state", 'models.Job.status.in_(("running", "claimed"))' in src and '"queued", "claimed", "running"' in src)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
