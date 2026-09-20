"""DB-lock resilience (v130): a busy SQLite writer must never take a page down.

SQLite has ONE writer. When the background sweep is mid-write, a request's own
small write (recording last-seen, marking a job seen, minting a settings row) can
hit the busy timeout and raise OperationalError. Under WAL the page's READS still
succeed, so the right behaviour is to drop the deferrable write and let the page
render — never a 500. This suite proves:

  · database.safe_commit  → True on success; False + rollback on "database is
    locked"/"busy"; RE-RAISES any other OperationalError (a real bug still surfaces)
  · auth_security.touch_seen  → never raises when the commit is locked
  · the background balance syncs commit PER ITEM (source check) so the one writer
    is released between network calls — the actual cause of the outage
  · the request-path write sites call safe_commit / degrade (source check)

fastapi/sqlalchemy are stubbed (not installed here); OperationalError is a real
Exception subclass so the except clauses execute for real."""
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

# ---- a REAL OperationalError so `except OperationalError` actually catches -------
class OperationalError(Exception):
    def __init__(self, msg="", orig=None):
        super().__init__(msg)
        self.orig = orig if orig is not None else msg

# ---- stub sqlalchemy enough to import app.database for real ----------------------
sa = _mod("sqlalchemy", create_engine=lambda *a, **k: _Any(), inspect=lambda *a, **k: _Any(), text=lambda s: s, event=_Any())
_mod("sqlalchemy.exc", OperationalError=OperationalError)
class _DeclBase:  # DeclarativeBase must be subclassable
    pass
_mod("sqlalchemy.orm", DeclarativeBase=_DeclBase, sessionmaker=lambda *a, **k: _Any(), Session=_Any)
sa.orm = sys.modules["sqlalchemy.orm"]
sa.exc = sys.modules["sqlalchemy.exc"]

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]

import importlib
database = importlib.import_module("app.database")

print("-- safe_commit --")

class FakeDB:
    def __init__(self, raise_on_commit=None):
        self.raise_on_commit = raise_on_commit
        self.committed = 0
        self.rolled_back = 0
    def commit(self):
        if self.raise_on_commit is not None:
            raise self.raise_on_commit
        self.committed += 1
    def rollback(self):
        self.rolled_back += 1

ok = FakeDB()
check("commit that succeeds → True, no rollback", database.safe_commit(ok) is True and ok.committed == 1 and ok.rolled_back == 0)

locked = FakeDB(raise_on_commit=OperationalError("(sqlite3.OperationalError) database is locked"))
check("locked commit → False + rollback (no exception escapes)", database.safe_commit(locked) is False and locked.rolled_back == 1)

busy = FakeDB(raise_on_commit=OperationalError("database is busy"))
check("busy commit → False + rollback", database.safe_commit(busy) is False and busy.rolled_back == 1)

other = FakeDB(raise_on_commit=OperationalError("no such table: users"))
raised = False
try:
    database.safe_commit(other)
except OperationalError:
    raised = True
check("a NON-lock OperationalError is re-raised (real bugs still surface)", raised and other.rolled_back == 0)

print("-- touch_seen never raises on a locked write --")
# auth_security needs fastapi.Request, itsdangerous, models, queries at import
_mod("fastapi", Request=_Any)
_mod("itsdangerous", BadSignature=Exception, TimestampSigner=_Any)
_mod("app.models")
_mod("app.queries")
auth_security = importlib.import_module("app.auth_security")

from datetime import datetime, timedelta

class U:
    def __init__(self):
        self.last_seen_at = None
        self.last_ip = ""
        self.last_ua = ""

class SeenDB(FakeDB):
    pass

u = U()
db_locked = SeenDB(raise_on_commit=OperationalError("database is locked"))
_raised = False
try:
    auth_security.touch_seen(db_locked, u, "1.2.3.4", "UA")
except Exception as e:  # noqa: BLE001
    _raised = True
check("touch_seen swallows a locked commit (page still renders)", not _raised and db_locked.rolled_back == 1)
check("touch_seen still set the fields in memory", u.last_ip == "1.2.3.4" and u.last_seen_at is not None)

u2 = U(); u2.last_seen_at = datetime.utcnow(); u2.last_ip = "9.9.9.9"
db_ok = SeenDB()
auth_security.touch_seen(db_ok, u2, "9.9.9.9", "UA")
check("touch_seen throttles (<5 min, same ip) → no write at all", db_ok.committed == 0 and db_ok.rolled_back == 0)

print("-- source: the sweep commits per item, request paths degrade --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
bal = read("app/balances.py")
# sync_bc_balances: the commit must be INSIDE the for-loop (8-space indent), not
# only the old once-at-the-end (4-space) commit that held the lock across every BC
bc = bal.split("def sync_bc_balances")[1].split("\ndef ")[0]
check("sync_bc_balances commits per BC (commit indented inside the loop)",
      "\n        db.commit()" in bc and "Release it each round" in bc, bc[-300:])
acct = bal.split("def sync_account_balances")[1].split("def ")[0]
check("sync_account_balances commits per page (before the next fetch)", "commit this page before fetching the next" in acct)

jp = read("app/routes/jobs_page.py")
check("jobs poller uses safe_commit (never 500s the 4s poll)", "safe_commit(db)" in jp and "from ..database import safe_commit" in jp)
asrc = read("app/auth_security.py")
check("touch_seen uses safe_commit", "from .database import safe_commit" in asrc and "safe_commit(db)" in asrc)
ss = read("app/settings_store.py")
check("get_settings mint uses safe_commit / degrades on lock", "safe_commit(db)" in ss and "except OperationalError" in ss)
mn = read("app/main.py")
check("login gate retries once on a lock, then a 503 (never a bare 500 / logout)",
      "except _OpErr:" in mn and "status_code=503" in mn and "Retry-After" in mn)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
