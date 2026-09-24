"""Inbox row collapsing (v135).

A batch of a thousand ads rejected together used to draw a thousand identical
"Ad … rejected (no reason returned)" rows. They now fold into ONE row per distinct
(title + message + account) carrying a count, so the page shows a handful of lines
and stays light. The group header still shows the TRUE total; a per-group cap folds
any excess into a "+N more" line.

Pure `_collapse_rows` (fastapi/sqlalchemy stubbed for import), plus template/JS asserts."""
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

print("-- _collapse_rows --")
def row(msg, where, at=0, level="err"):
    return {"title": "Ad rejected", "message": msg, "where": where, "level": level, "at": at, "fix": []}

# 1000 identical rejections on one account + 3 on another + one unique
rows = [row("Ad “X” rejected (no reason returned).", "blue bat_1", at=i) for i in range(1000)]
rows += [row("Ad “X” rejected (no reason returned).", "blue bat_2", at=i) for i in range(3)]
rows += [row("Ad “Y” rejected: nudity.", "blue bat_2", at=5)]
collapsed, hidden = ib._collapse_rows(rows)
check("folds to one row per (title, message, account)", len(collapsed) == 3 and hidden == 0, len(collapsed))
big = next(c for c in collapsed if c["where"] == "blue bat_1")
check("the folded row carries the count", big["count"] == 1000)
check("keeps the newest timestamp of the bucket", big["at"] == 999, big["at"])
small = next(c for c in collapsed if c["message"].endswith("nudity."))
check("a one-off stays count 1", small["count"] == 1)
tot = sum(c["count"] for c in collapsed)
check("no issue is lost — counts sum back to the input", tot == len(rows) == 1004, tot)

# the per-group cap folds excess distinct rows into a remainder
many = [row("m%d" % i, "acct%d" % i) for i in range(ib.GROUP_ROW_CAP + 25)]
cap_rows, cap_hidden = ib._collapse_rows(many)
check("distinct rows past the cap fold into a 'hidden' remainder", len(cap_rows) == ib.GROUP_ROW_CAP and cap_hidden == 25, (len(cap_rows), cap_hidden))

print("-- route wires total + collapse --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
r = read("app/routes/inbox.py")
check("route records the true total, then collapses", 'g["total"] = len(g["rows"])' in r and 'g["rows"], g["hidden"] = _collapse_rows(g["rows"])' in r)

print("-- template + JS --")
t = read("app/templates/inbox.html")
check("header shows the true total (data-total), group fix uses total > 1", 'data-total="{{ g.total }}"' in t and ">{{ g.total }}<" in t and "g.total > 1" in t)
check("a folded row shows a × count chip and carries data-count", 'data-count="{{ it.count or 1 }}"' in t and "× {{ it.count }}" in t and "it.count > 1" in t)
check("a '+N more' line when the cap folds extra kinds", "{% if g.hidden %}" in t and "more kind" in t)
check("JS: unfiltered → true total; filtering → sum of visible folded counts",
      "el.dataset.total" in t and 'parseInt(r.dataset.count' in t and "reduce(function" in t)
check("STATIC_VERSION bumped", 'STATIC_VERSION = "172"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
