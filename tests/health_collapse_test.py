"""Health page (/monitor) Issues-table row collapsing (v136).

The Health page rendered ONE DOM row per issue, so an account with 1,041 rejected
ads drew 1,041 identical "Ad rejected" rows — the thing the user was looking at.
It now folds identical (title + message + account) rows into a single "× N" line
using the SAME helper the Inbox uses, while the KPI strip / kind chips keep the TRUE
totals. Excess distinct rows past the cap fold into a "+N more" line.

Pure `_collapse_rows` behaviour (fastapi/sqlalchemy stubbed for import), plus route +
template asserts that the Health table is wired to the folded rows, not the raw list."""
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

class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
    def __getattr__(self, n): return _Any()

_mod("fastapi", APIRouter=_Any, Depends=_Any(), Request=_Any)
_mod("fastapi.responses", RedirectResponse=_Any)
_mod("sqlalchemy.orm", Session=_Any)
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.inbox", build=lambda *a, **k: [], counts=lambda *a: {})
_mod("app.models"); _mod("app.database", get_db=lambda: None); _mod("app.templating", render=lambda *a, **k: None)

import importlib
ib = importlib.import_module("app.routes.inbox")

print("-- the helper the Health table relies on --")
def row(msg, where, at=0, level="err", kind="issue_ad"):
    return {"id": "issue:1", "title": "Ad rejected", "message": msg, "where": where,
            "level": level, "kind": kind, "at": at, "fix": [], "ack": False}

# one account with 1,041 identical rejections + a couple of distinct ones
rows = [row("Ad “” rejected (no reason returned).", "blue bat_260706153622", at=i) for i in range(1041)]
rows += [row("Ad “Y” rejected: nudity.", "blue bat_260706153622", at=5)]
rows += [row("Out of funds ($0.00) with ACTIVE campaigns.", "green owl", level="err", kind="issue_payment")]
collapsed, hidden = ib._collapse_rows(rows)
check("1,041 identical rows fold to one", any(c["count"] == 1041 for c in collapsed), [c["count"] for c in collapsed])
check("distinct messages survive the fold", len(collapsed) == 3 and hidden == 0, (len(collapsed), hidden))
check("every folded row carries a count (template reads it unconditionally)", all("count" in c for c in collapsed))
check("no issue is lost — counts sum back to the input", sum(c["count"] for c in collapsed) == len(rows) == 1043)
big = next(c for c in collapsed if c["count"] == 1041)
check("the folded row keeps the newest timestamp", big["at"] == 1040, big["at"])
check("the folded row keeps its kind + level (filters still work)", big["kind"] == "issue_ad" and big["level"] == "err")

print("-- route wiring --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
m = read("app/routes/monitor.py")
check("monitor folds items with the shared helper", "from .inbox import _collapse_rows" in m and "_collapse_rows(items)" in m)
check("monitor passes issue_rows + issue_hidden to the template", '"issue_rows": issue_rows' in m and '"issue_hidden": issue_hidden' in m)
check("counts/kinds still built from the FULL items (true totals)", "inbox_mod.counts(items)" in m)

print("-- template --")
t = read("app/templates/monitor.html")
check("the Issues table iterates the FOLDED rows, not the raw items", "{% for it in issue_rows %}" in t and "{% for it in items %}" not in t)
check("a folded row shows a × count chip and carries data-count", 'data-count="{{ it.count }}"' in t and "× {{ it.count }}" in t and "it.count > 1" in t)
check("a '+N more' line appears when the cap folds extra rows", "{% if issue_hidden %}" in t and "more distinct message" in t)
check("STATIC_VERSION bumped", 'STATIC_VERSION = "160"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
