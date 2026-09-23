"""v155.1 — (1) copying forms/pages to accounts the stored TikTok login can't reach: one clear
line with the fix instead of 68 truncated copies, no wasted re-reads; (2) the Assets page
crashed after an audit that stopped early (snapshot without totals)."""
import ast, importlib, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

print("-- no access for the cookie login --")
sys.modules.setdefault("app.diag", types.SimpleNamespace(record=lambda *a, **k: None))
web = importlib.import_module("app.instant_page_web")
labels = ["BLUE BAT CAFE LLC_bucmdv", "BLUE BAT CAFE LLC_o1n6g2", "blue bat07"] + [f"acct{i}" for i in range(65)]
line = web.no_access_summary(labels, ["BLUE BAT CAFE LLC"] * len(labels))
check("one line: how many, which BC, the first names, +N more", line.startswith("68 accounts in BLUE BAT CAFE LLC")
      and "BLUE BAT CAFE LLC_bucmdv, BLUE BAT CAFE LLC_o1n6g2, blue bat07 +65 more" in line, line[:160])
check("…and the fix is in it (not cut off after 400 characters of repeats)", "Fix:" in line and len(line) < 900 and web.is_no_access(line))
check("one account reads in the singular", web.no_access_summary(["A"]).startswith("1 account — "))
denied = {"code": 100000, "msg": "Internal system error", "data": {"err_msg": "RPCError{Method:[GetLoginAdvInfoByUid] BizStatusMessage:[not any access permission]}"}}
check("a single failure explains it with the fix too", web.is_no_access(web.explain(denied, "7658")) and "Fix:" in web.explain(denied, "7658"))

calls = []
def fake_post(path, body, target, params=None):
    calls.append(path)
    return denied
web._post = fake_post
r = web.duplicate("111", "Form", "7658258034419515393", source_owner="999")
check("no access: stops after ONE read — nothing created, other read shapes not tried",
      r["ok"] is False and r.get("no_access") is True and calls == ["/v1/page_info/111/"], calls)
check("…and no longer mislabelled 'read source' (it is the TARGET account the login can't see)", not r["error"].startswith("read source"))

lf, ip, jh = read("app/routes/lead_forms.py"), read("app/routes/instant_pages.py"), read("app/job_handlers.py")
check("form copy: a no-access account skips the re-read sync and the 1.5 s pause",
      'if r.get("no_access"):\n        return r.get("error", "")' in lf and "no_access.append((label, bc_names.get(acct.owner_bc_id" in lf
      and "_time.sleep(0.3)\n            continue" in lf)
check("both copy jobs put the summary line FIRST", lf.count("failed.insert(0, instant_page_web.no_access_summary(") == 1
      and ip.count("failed.insert(0, instant_page_web.no_access_summary(") == 1)
check("job notice counts every failed account and keeps room for the fix", "def _n_failed(r: dict)" in jh and jh.count('" · ".join(r["failed"])[:900]') == 2)
ns = {}
for node in ast.parse(jh).body:
    if isinstance(node, ast.FunctionDef) and node.name == "_n_failed":
        exec(compile(ast.Module([node], []), "jh", "exec"), ns)
check("…68 no-access + 2 other = 70 failed", ns["_n_failed"]({"failed": ["summary", "x", "y"], "no_access": 68}) == 70
      and ns["_n_failed"]({"failed": ["x"]}) == 1)

print("-- Assets page after an unfinished audit --")
src = read("app/bc_assets.py")
ns = {}
for node in ast.parse(src).body:
    if (isinstance(node, ast.FunctionDef) and node.name == "merge_partial") or \
       (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "EMPTY_SUMMARY" for t in node.targets)):
        exec(compile(ast.Module([node], []), "bca", "exec"), ns)
mp = ns["merge_partial"]
prev = {"at": "2026-09-22T10:00:00", "summary": {"accounts": 40, "ready": 30}, "accounts": [{"advertiser_id": "1"}], "errors": [], "main_bc": "M"}
stopped = {"at": "2026-09-23T12:00:00", "errors": ["stopped"], "bcs": [], "accounts": [], "main_bc": "M"}
out = mp(prev, stopped)
check("a stopped audit keeps the last complete one (numbers and table)", out["summary"]["accounts"] == 40 and out["accounts"] == prev["accounts"])
check("…with this run's notes and a line saying so", out["errors"][0] == "stopped" and "didn't finish" in out["errors"][1])
first = mp({}, {"at": "x", "errors": ["No main Business Center chosen yet"], "accounts": []})
check("no earlier audit: totals are zeros, never missing", first["summary"]["accounts"] == 0 and first["summary"]["ready"] == 0)
check("only the finished scan stores as complete; every early exit merges",
      src.count("return _store(db, snap, user_id, complete=True)") == 1 and "if not complete:\n        snap = merge_partial(snapshot(db, user_id), snap)" in src)
check("the page never reads totals that aren't there", "{% if snap.at and snap.summary %}" in read("app/templates/bc_assets.html"))

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
