"""v155.33 — the future-start watch: an ad group of a dashboard launch that TikTok holds for
later (start > 20 min after creation) raises one Inbox notice on the next sweep. Pure gap maths
+ a fake session for the watch."""
import ast, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

src = read("app/adgroup_stats.py")
ns = {"Session": object, "log": types.SimpleNamespace(exception=lambda *a, **k: None), "models": types.SimpleNamespace(AdAccount=object)}
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name in ("future_start_gap_min", "watch_future_starts") or \
       (isinstance(node, (ast.Assign, ast.AnnAssign)) and any(getattr(t, "id", "") in ("FUTURE_MIN", "_future_seen") for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))):
        exec(compile(ast.Module([node], []), "ags", "exec"), ns)

print("-- the gap --")
gap = ns["future_start_gap_min"]
check("Karachi case: created 10:02, start 15:03 → 301 min", round(gap({"create_time": "2026-09-25 10:02:22", "schedule_start_time": "2026-09-25 15:03:22"})) == 301)
check("a normal launch: +1 min", round(gap({"create_time": "2026-09-25 10:02:22", "schedule_start_time": "2026-09-25 10:03:22"})) == 1)
check("missing / unreadable → 0, never an error", gap({}) == 0.0 and gap({"create_time": "x", "schedule_start_time": "y"}) == 0.0)

print("-- the watch --")
class Col:
    def __init__(self, n): self.n = n
    def __eq__(self, v): return ("eq", self.n, v)
    def like(self, v): return ("like", self.n, v.strip("%"))
    __hash__ = object.__hash__
class Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *c):
        rows = self.rows
        for k, n, v in c:
            rows = [r for r in rows if (getattr(r, n) == v if k == "eq" else v in str(getattr(r, n)))]
        return Q(rows)
    def first(self): return self.rows[0] if self.rows else None
    def __iter__(self): return iter([(r.campaign_id,) for r in self.rows])
class DB:
    def __init__(self): self.logs = []; self.alerts = []; self.commits = 0
    def query(self, what): return Q(self.logs if what is M.LaunchLog.campaign_id else self.alerts)
    def add(self, a): self.alerts.append(a)
    def commit(self): self.commits += 1
M = types.SimpleNamespace(
    LaunchLog=types.SimpleNamespace(campaign_id=Col("campaign_id"), advertiser_id=Col("advertiser_id"), ok=Col("ok")),
    Alert=types.SimpleNamespace(id=Col("id"), kind=Col("kind"), ref_id=Col("ref_id"), message=Col("message")))
ns["models"] = M
class AlertRow:
    def __init__(self, **kw): self.__dict__.update(kw); self.id = 1
M.Alert.__call__ = None
ns["models"] = types.SimpleNamespace(LaunchLog=M.LaunchLog, Alert=type("Alert", (), {"id": Col("id"), "kind": Col("kind"), "ref_id": Col("ref_id"), "message": Col("message"),
                                                                                      "__init__": lambda self, **kw: self.__dict__.update(kw)}))
db = DB()
db.logs = [types.SimpleNamespace(campaign_id="C1", advertiser_id="A", ok=True)]
acct = types.SimpleNamespace(advertiser_id="A", advertiser_name="FABU +5|29526")
groups = [{"adgroup_id": "G1", "campaign_id": "C1", "adgroup_name": "USA Playful #18", "operation_status": "ENABLE", "create_time": "2026-09-25 10:02:22", "schedule_start_time": "2026-09-25 15:03:22"},
          {"adgroup_id": "G2", "campaign_id": "C1", "operation_status": "ENABLE", "create_time": "2026-09-25 10:02:22", "schedule_start_time": "2026-09-25 10:03:22"},
          {"adgroup_id": "G3", "campaign_id": "HAND", "operation_status": "ENABLE", "create_time": "2026-09-25 10:02:22", "schedule_start_time": "2026-09-26 10:03:22"}]
n = ns["watch_future_starts"](db, acct, groups)
check("the held ad group of OUR launch raises one Inbox notice, naming the account, the hours and the ad group",
      n == 1 and len(db.alerts) == 1 and "5.0 h after" in db.alerts[0].message and "ad group G1" in db.alerts[0].message and db.alerts[0].kind == "account_error" and db.alerts[0].ref_id == "A", str([a.__dict__ for a in db.alerts]))
check("a normal +1 min ad group and a hand-built campaign's schedule are left alone", not any("G2" in a.message or "G3" in a.message for a in db.alerts))
check("the next sweep doesn't raise it again", ns["watch_future_starts"](db, acct, groups) == 0 and len(db.alerts) == 1)
check("wired into the sync's per-account write (same listing, no extra TikTok call)", "watch_future_starts(db, acct, groups)" in src.split("def write_account")[1][:2500])
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
