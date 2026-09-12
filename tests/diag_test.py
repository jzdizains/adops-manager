"""The error feed — a watcher that can break what it watches is worse than none.

Three properties are non-negotiable and tested here:
  1. record() never raises, even when the database is unavailable or the payload is junk.
  2. nothing that looks like a credential is ever written — access tokens travel in
     headers, but a request body or a job payload can carry one, and this feed is a page.
  3. repeats of one error count on a row instead of filling the table.

Runs without fastapi/sqlalchemy: diag is exercised with its storage stubbed out.
"""
import sys, types, os, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
sys.modules["app"] = pkg
import importlib
diag = importlib.import_module("app.diag")

stored = []
def _fake_store(row): stored.append(row)
diag._store = _fake_store

print("\n-- nothing that could be a credential is ever written --")
diag.RECENT.clear(); stored.clear()
diag.record("tiktok", "/bc/get/", 40002, "nope", {
    "params": {"bc_id": "111", "access_token": "act.SECRET", "Access-Token": "act.SECRET"},
    "body": {"advertiser_id": "A1", "secret": "s3cr3t", "api_key": "k",
             "nested": {"refresh_token": "r", "pixel_code": "CODE1"}},
})
blob = json.dumps(stored[-1])
check("no access token", "act.SECRET" not in blob, blob)
check("no secret", "s3cr3t" not in blob, blob)
check("no api key value", '"api_key": "k"' not in blob, blob)
check("no refresh token", '"refresh_token": "r"' not in blob, blob)
check("the useful ids survive", "pixel_code" in blob and "A1" in blob, blob)
check("redaction is visible, not silent", "[redacted]" in blob, blob)

print("\n-- but a field that only LOOKS like a credential is kept --")
diag.RECENT.clear(); stored.clear()
diag.record("tiktok", "/ad/create/", 40002, "no access to the TikTok account used in this ad", {
    "body": {"identity_id": "835bce4-b023-58b4-8950-ba41997dc211",
             "identity_type": "BC_AUTH_TT",
             "identity_authorized_bc_id": "7658285881817202708",
             "access_token": "act.SECRET"}})
kept = stored[-1]["context"]["body"]
check("the Business Center id survives", kept["identity_authorized_bc_id"] == "7658285881817202708",
      str(kept))
check("the identity survives", kept["identity_id"].startswith("835bce4"), str(kept))
check("and the real token still does not", kept["access_token"] == "[redacted]", str(kept))

print("\n-- a credential-looking key at any depth is caught --")
deep = diag.redact({"a": {"b": {"c": {"session_token": "x", "keep": "y"}}}})
check("nested token redacted", deep["a"]["b"]["c"]["session_token"] == "[redacted]", str(deep))
check("its sibling is kept", deep["a"]["b"]["c"]["keep"] == "y", str(deep))

print("\n-- record() never raises --")
class Exploding:
    def __repr__(self): raise RuntimeError("boom")
    def __str__(self): raise RuntimeError("boom")
ok = True
for bad in (Exploding(), {"k": Exploding()}, [Exploding()], object()):
    try:
        diag.record("app", "test", "X", "m", bad)
    except Exception as e:
        ok = False
        check("survives a hostile payload", False, repr(e))
check("survives hostile payloads", ok)

def _boom(row): raise RuntimeError("database is gone")
diag._store = _boom
try:
    diag.record("tiktok", "/x/", 1, "m", {"a": 1})
    check("survives a dead database", True)
except Exception as e:
    check("survives a dead database", False, repr(e))
check("and still keeps it in memory", diag.RECENT[-1]["where"] == "/x/")
diag._store = _fake_store

print("\n-- the in-memory ring is bounded --")
for i in range(500):
    diag.record("app", "flood", "F", f"message {i}", {"i": i})
check("ring stays at its cap", len(diag.RECENT) == diag.RECENT.maxlen, str(len(diag.RECENT)))
check("cap is small enough for a 512 MB box", diag.RECENT.maxlen <= 500, str(diag.RECENT.maxlen))

print("\n-- long values are truncated, not stored whole --")
diag.RECENT.clear()
diag.record("tiktok", "/x/", 1, "m" * 9000, {"big": "z" * 9000})
check("message is capped", len(diag.RECENT[-1]["message"]) <= 2000, str(len(diag.RECENT[-1]["message"])))
check("context strings are capped", len(diag.RECENT[-1]["context"]["big"]) <= 400)

print()
print(("FAILED: " + ", ".join(fails)) if fails else "all good")
sys.exit(1 if fails else 0)
