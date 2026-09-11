"""Business Centers with no finance role must be asked once, not forever.

TikTok answers "You don\'t have finance permission. Please check your finance role." for a
BC where the user is an Admin but holds no finance role. The balance sweeps used to ask
every one of them on every pass — two wasted calls per BC, and the same error in the feed
for ever. The refusal is now remembered, and retried after a week so a role granted later
starts working without anyone remembering to clear anything.
"""
import sys, types, os
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

if "sqlalchemy" not in sys.modules:
    sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm")
    class Session: pass
    orm.Session = Session; sa.orm = orm
    sys.modules["sqlalchemy"], sys.modules["sqlalchemy.orm"] = sa, orm

pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
sys.modules["app"] = pkg
for name in ("config", "models", "queries", "tiktok_api"):
    sys.modules[f"app.{name}"] = types.ModuleType(f"app.{name}")
STORE = {}
sys.modules["app.queries"].get_setting = lambda db, k, d="": STORE.get(k, d)
sys.modules["app.queries"].set_setting = lambda db, k, v: STORE.__setitem__(k, v)
sys.modules["app.config"].BC_LOW_BALANCE_THRESHOLD = 50.0

import importlib
balances = importlib.import_module("app.balances")
db = object()

print("\n-- an unrelated error is not a finance refusal --")
STORE.clear()
check("a rate limit is not remembered",
      balances.note_finance_refusal(db, "111", "Too many requests") is False)
check("and nothing is skipped", balances.finance_skip(db) == set(), str(balances.finance_skip(db)))

print("\n-- a finance refusal is remembered and skipped --")
check("recognised", balances.note_finance_refusal(
    db, "7143990070255550465", "You don\'t have finance permission. Please check your finance role.") is True)
check("that BC is skipped next sweep", balances.finance_skip(db) == {"7143990070255550465"},
      str(balances.finance_skip(db)))
check("other BCs are untouched", "111" not in balances.finance_skip(db))

print("\n-- but it is retried after a week, so a granted role heals itself --")
import json
old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)).isoformat(timespec="seconds")
STORE[balances.FINANCE_BLOCK_KEY] = json.dumps({"7143990070255550465": old})
check("a stale block expires", balances.finance_skip(db) == set(), str(balances.finance_skip(db)))

print("\n-- a corrupt setting never breaks a sweep --")
STORE[balances.FINANCE_BLOCK_KEY] = "{not json"
check("bad JSON is survived", balances.finance_skip(db) == set())
STORE[balances.FINANCE_BLOCK_KEY] = json.dumps({"111": "not-a-date"})
check("a bad timestamp is survived", balances.finance_skip(db) == set())

print()
print(("FAILED: " + ", ".join(fails)) if fails else "all good")
sys.exit(1 if fails else 0)
