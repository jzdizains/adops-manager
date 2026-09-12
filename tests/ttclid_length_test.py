"""How long a TikTok click id we are willing to keep.

The cap used to be 500, chosen without evidence. That is the dangerous kind of guess:
a truncated ttclid is not a shorter ttclid, it is a WRONG one. It stores cleanly, passes
the macro check, looks fine in the database, and is then silently unmatched by the Events
API with nothing anywhere naming the cause.

The cap is now well above anything TikTok is known to send, and a click id long enough to
be interesting is recorded in Diagnostics rather than quietly trimmed.

Runs without fastapi/sqlalchemy.
"""
import sys, types, os

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
sys.modules["app.models"] = types.ModuleType("app.models")

noted = []
diag = types.ModuleType("app.diag")
diag.record = lambda kind, where, code, message, context=None, request_id="": noted.append(
    {"where": where, "code": code, "message": message, "context": context})
sys.modules["app.diag"] = diag

import importlib
tracking = importlib.import_module("app.tracking")

print("\n-- the cap is far above any plausible click id --")
check("it is no longer 500", tracking.TTCLID_MAX != 500, str(tracking.TTCLID_MAX))
check("and is generous", tracking.TTCLID_MAX >= 2000, str(tracking.TTCLID_MAX))
check("a typical 180-char click id is untouched",
      tracking._clean("x" * 180, tracking.TTCLID_MAX) == "x" * 180)
check("a 900-char one now survives whole",
      len(tracking._clean("y" * 900, tracking.TTCLID_MAX)) == 900,
      str(len(tracking._clean("y" * 900, tracking.TTCLID_MAX))))

print("\n-- an unusually long one is reported before it can be cut --")
noted.clear()
tracking.note_long_ttclid("z" * 700, "click")
check("it is recorded", len(noted) == 1, str(noted))
check("with the real length", noted and noted[0]["context"]["length"] == 700, str(noted))
check("and says where to look", noted and "Events API matching" in noted[0]["message"],
      str(noted and noted[0]["message"]))

print("\n-- an ordinary one is not noise --")
noted.clear()
tracking.note_long_ttclid("a" * 180, "click")
check("nothing recorded", not noted, str(noted))
noted.clear()
tracking.note_long_ttclid("", "click")
check("an empty one records nothing", not noted)

print("\n-- the note can never break a click --")
diag.record = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("diag is down"))
try:
    tracking.note_long_ttclid("q" * 900, "click")
    check("a broken feed is survived", True)
except Exception as e:
    check("a broken feed is survived", False, repr(e))
diag.record = lambda kind, where, code, message, context=None, request_id="": noted.append({"where": where})

print("\n-- unreplaced macros are still refused, whatever the length --")
for bad in ("__CLICKID__", "{ttclid}", "{{ttclid}}"):
    check(f"{bad} is rejected", tracking._clean(bad, tracking.TTCLID_MAX) == "", bad)

print()
print(("FAILED: " + ", ".join(fails)) if fails else "all good")
sys.exit(1 if fails else 0)
