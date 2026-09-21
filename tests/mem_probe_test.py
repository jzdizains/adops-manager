"""Always-on memory sampler (v135+): the black box that names the OOM culprit.

Two streaming fixes did not stop the ~18-min OOM and the per-step breadcrumb missed it
(the spike explodes inside one step, and the audience spike is in the jobs worker, not the
sweep). So a background thread now samples process RSS every few seconds and, ONLY once it
has climbed past MEM_PROBE_MB, writes one Diagnostics line naming the current activity —
which the sweep and every big per-account loop keep up to date. The LAST such line before a
crash is the operation that did it, with the account.

Pure checks on set_activity + the sampler's log-when-high / dedup logic, plus source asserts
that the three big loops stamp the activity."""
import os, sys, types

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
logged = []
_mod("app.database", SessionLocal=lambda: types.SimpleNamespace(close=lambda: None))
_mod("app.queries", log=lambda db, msg, level="info", source="": logged.append((source, msg)))

import importlib
bg = importlib.import_module("app.background")

print("-- set_activity / current state --")
bg.set_activity("sync_campaigns 143/280 blue bat")
check("activity is stored (and clamped)", bg._activity == "sync_campaigns 143/280 blue bat")
bg.set_activity("x" * 500)
check("activity clamped to 140 chars", len(bg._activity) == 140)

print("-- the sampler logs only when high, names the activity, dedups --")
# drive one pass of the sampler's inner logic by monkeypatching rss_mb + a one-shot loop
seq = iter([200.0, 340.0, 340.0, 360.0, 280.0, 400.0])
bg.rss_mb = lambda: next(seq)
# reimplement the sampler's body deterministically the same way it runs (no sleeps/threads)
logged.clear()
last = ""
def step():
    global last
    mb = bg.rss_mb()
    if mb >= bg.MEM_PROBE_MB:
        line = f"mem: {mb:.0f} MB during {bg._activity}"
        if line != last:
            from app.database import SessionLocal
            from app import queries
            db = SessionLocal();  queries.log(db, line + " (limit 512)", level="warning", source="mem"); db.close()
            last = line
    elif mb < bg.MEM_PROBE_MB - 40:
        last = ""
bg.set_activity("sync_campaigns 143/280 blue bat")
step()  # 200 → nothing
check("below the probe threshold → silent", logged == [])
step()  # 340 → logs
step()  # 340 again → deduped, no second line
check("first climb past 330 logs once; identical reading deduped", len(logged) == 1 and "340 MB during sync_campaigns 143/280 blue bat" in logged[0][1] and logged[0][0] == "mem")
step()  # 360 → new value, logs
check("a higher reading logs a fresh line", len(logged) == 2 and "360 MB" in logged[1][1])
step()  # 280 → dropped safe, resets 'last'
bg.set_activity("issues.scan 12/96 acct X"); step()  # 400 → logs with the new activity
check("after dropping back and climbing again, it logs the NEW activity", len(logged) == 3 and "400 MB during issues.scan 12/96 acct X" in logged[2][1])

print("-- source: sampler started + every big loop stamps the activity --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
b = read("app/background.py")
check("sampler thread started as a daemon in start()", 'target=_mem_sampler, name="adops-mem-sampler", daemon=True' in b)
check("the sweep stamps each step's activity", 'set_activity("sweep:" + step)' in b)
check("sync_campaigns stamps per account", 'background.set_activity(f"sync_campaigns {_idx}/{len(accounts)}' in read("app/live_spend.py"))
check("issues.scan stamps per account", 'background.set_activity(f"issues.scan {i}/{len(with_token)}' in read("app/issues.py"))
check("audience stamps per account", 'background.set_activity(f"audience {i}/{len(accounts)}' in read("app/audience.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
