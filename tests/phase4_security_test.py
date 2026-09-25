"""Phase 4a (v148) — security engine upgrades.

  * Secrets sealed at rest (AES-256-GCM): tokens, 2FA secrets, the Events API token, the
    cookie + mailbox files. Legacy plaintext still reads; a wrong key never reads as "".
  * TOTP replay guard: each code works once.
  * Real client IP: X-Forwarded-For read from the RIGHT (TRUSTED_PROXY_HOPS).
  * Lockout keyed on IP, (email, IP) and — much later — the email alone.
  * Cross-site POSTs refused (Origin / Referer / Sec-Fetch-Site).
  * Server-side sessions with per-device sign-out; logout is POST only.
  * The PIN gate is live and its redirect can't leave the site.
  * Audit trail of every change; app log / sessions / audit pruned; logging configured.
"""
import base64, importlib, json, os, sys, tempfile, time, types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
FUT = "from __future__ import annotations\n"
def grab(src, name):
    i = src.index("def " + name + "(")
    ends = [e for e in (src.find(m, i + 1) for m in ("\ndef ", "\n@", "\nclass ", "\n# ----", "\nUNSAFE_", "\nCSRF_", "\nEMAIL_", "\nPIN_PATHS", "\nTRUST_")) if e != -1]
    return src[i:(min(ends) if ends else len(src))]

# sqlalchemy isn't installed here: a stand-in TypeDecorator so secrets_box imports
if "sqlalchemy" not in sys.modules:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        sa = types.ModuleType("sqlalchemy"); sat = types.ModuleType("sqlalchemy.types")
        class TypeDecorator:
            def __init__(self, *a, **k): pass
        sat.TypeDecorator, sat.Text = TypeDecorator, object
        sys.modules["sqlalchemy"], sys.modules["sqlalchemy.types"] = sa, sat
os.environ["SECRETS_KEY"] = base64.urlsafe_b64encode(b"k" * 32).decode()
sb = importlib.import_module("app.secrets_box")
sb.reset_keys()

# =======================================================================================
print("-- secrets at rest --")
tok = "act.5d1c0ffee-live-token"
a, b = sb.seal(tok), sb.seal(tok)
check("sealed values carry the prefix and differ every time (random nonce)", a.startswith("enc:v1:") and a != b and tok not in a)
check("…and open back to the plaintext", sb.unseal(a) == tok == sb.unseal(b))
check("empty / None / already-sealed pass through untouched", sb.seal("") == "" and sb.seal(None) is None and sb.seal(a) == a)
check("a pre-encryption plaintext value still reads (and is marked for sealing)", sb.unseal("legacy-token") == "legacy-token" and sb.needs_reseal("legacy-token"))
et = sb.EncryptedText()
check("the column type seals on write and opens on read", et.process_result_value(et.process_bind_param(tok, None), None) == tok
      and et.process_bind_param(tok, None).startswith("enc:v1:"))
tampered = a[:-4] + ("AAAA" if not a.endswith("AAAA") else "BBBB")
check("a tampered value doesn't open — and comes back sealed, never as '' (a lost key must not read as '2FA off')",
      sb.unseal(tampered) == tampered)
old_sealed = a
os.environ["SECRETS_KEY_OLD"] = os.environ["SECRETS_KEY"]
os.environ["SECRETS_KEY"] = base64.urlsafe_b64encode(b"n" * 32).decode()
sb.reset_keys()
check("rotation: the old key (SECRETS_KEY_OLD) still opens it, and it's marked for re-sealing",
      sb.unseal(old_sealed) == tok and sb.needs_reseal(old_sealed) and not sb.needs_reseal(sb.seal(tok)))
os.environ.pop("SECRETS_KEY_OLD")
sb.reset_keys()
check("without the old key it stays sealed", sb.unseal(old_sealed) == old_sealed)
tmp = Path(tempfile.mkdtemp()) / "cookies.json"
sb.write_json(tmp, {"cookies": {"sessionid": "abc"}})
raw = tmp.read_text()
check("files are sealed on disk and read back", raw.startswith("enc:v1:") and "sessionid" not in raw and sb.read_json(tmp) == {"cookies": {"sessionid": "abc"}})
tmp.write_text(json.dumps({"cookies": {"old": "1"}}))
check("an old plaintext file still reads", sb.read_json(tmp) == {"cookies": {"old": "1"}})
check("the Events API token inside settings JSON is sealed / opened",
      sb.settings_unseal(sb.settings_seal({"events_access_token": "EA1", "x": 1})) == {"events_access_token": "EA1", "x": 1}
      and sb.settings_seal({"events_access_token": "EA1"})["events_access_token"].startswith("enc:v1:"))
md = read("app/models.py")
check("columns sealed: account + BC tokens, refresh token, TOTP secret",
      md.count("Column(EncryptedText") == 7 and "totp_secret = Column(EncryptedText" in md and "refresh_token = Column(EncryptedText" in md)
check("existing plaintext is sealed at start (raw SQL, idempotent), files too",
      "_seal()" in read("app/main.py") and "def seal_existing(engine)" in read("app/secrets_box.py") and "seal_files()" in read("app/database.py"))
check("cookie + mailbox files written sealed", "secrets_box.write_json(target, payload)" in read("app/spark_web_api.py") and "target = config.COOKIE_FILE if" in read("app/spark_web_api.py")
      and "secrets_box.write_json(INVITE_MAIL_FILE" in read("app/invite_mail.py"))
check("settings rows are sealed on every write", read("app/settings_store.py").count("json.dumps(_sealed(") == 5)
check("cryptography is a requirement", "cryptography>=" in read("requirements.txt"))

# =======================================================================================
print("-- auth pieces --")
sec_src = read("app/auth_security.py")
import hmac, hashlib, struct
ns = {"hmac": hmac, "hashlib": hashlib, "struct": struct, "base64": base64, "time": time, "LOCK_AFTER": 5, "LOCK_MIN": 15}
for fn in ("_hotp", "totp_step", "totp_ok", "totp_accept", "ip_from_chain", "lock_minutes"):
    exec(FUT + grab(sec_src, fn), ns)
ns["LOCK_AFTER"], ns["LOCK_MIN"] = 5, 15
secret = base64.b32encode(b"12345678901234567890").decode()
now = 1_800_000_000
code = ns["_hotp"](secret, now // 30)
class U: totp_secret, totp_last_step = secret, 0
class D:
    commits = 0
    def commit(self): D.commits += 1
u = U()
check("a fresh code logs in", ns["totp_accept"](D(), u, code, at=now) and u.totp_last_step == now // 30)
check("the same code again is refused (replay)", not ns["totp_accept"](D(), u, code, at=now + 5))
check("…and so is the previous step's code", not ns["totp_accept"](D(), u, ns["_hotp"](secret, now // 30 - 1), at=now + 5))
check("the next step's code works", ns["totp_accept"](D(), u, ns["_hotp"](secret, now // 30 + 1), at=now + 31))
check("an undecryptable secret never matches and never raises", ns["totp_step"]("enc:v1:xx==", "123456") is None)
ipc = ns["ip_from_chain"]
check("X-Forwarded-For: the proxy's own entry (rightmost) wins — a spoofed left entry is ignored",
      ipc("6.6.6.6, 203.0.113.9", "10.0.0.1", 1) == "203.0.113.9")
check("two proxies (Cloudflare → Render): second from the right", ipc("6.6.6.6, 203.0.113.9, 172.70.1.1", "10.0.0.1", 2) == "203.0.113.9")
check("no proxy configured: the socket's peer", ipc("6.6.6.6", "198.51.100.4", 0) == "198.51.100.4")
check("config: Render = 1 hop by default", 'TRUSTED_PROXY_HOPS = max(int(os.environ.get("TRUSTED_PROXY_HOPS", "1" if ON_SERVER else "0")), 0)' in read("app/config.py"))
lm = ns["lock_minutes"]
check("lock length: 15 min at 5, ×2 at 10, ×4 at 15; the email-wide lock starts at 20",
      (lm(4), lm(5), lm(10), lm(15), lm(19, 20), lm(20, 20)) == (0, 15, 30, 60, 0, 15))
lf = grab(sec_src, "locked_for")
check("lockout keyed on IP, (email, IP) and the email alone (EMAIL_LOCK_AFTER)",
      "I == ip), I == ip)" in lf and "after=EMAIL_LOCK_AFTER" in lf and "EMAIL_LOCK_AFTER = 20" in sec_src)
check("login + 2FA use the replay guard", "sec.totp_accept(db, user, code)" in read("app/routes/auth.py")
      and "sec.totp_accept(db, me, code)" in read("app/routes/settings_page.py"))

# =======================================================================================
print("-- cross-site requests --")
exec(FUT + "UNSAFE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')\nCSRF_EXEMPT = ('/postback', '/t/', '/pub/src/', '/oauth/callback', '/health')\n" + grab(sec_src, "origin_ok"), ns)
ok = ns["origin_ok"]
H = "dash.example.com"
check("same-site POST (Origin matches) passes", ok("POST", "/status/pause", H, origin="https://dash.example.com"))
check("a POST from another site is refused", not ok("POST", "/status/pause", H, origin="https://evil.example"))
check("…also when only the Referer tells", not ok("POST", "/settings/save", H, referer="https://evil.example/x"))
check("…and when only Sec-Fetch-Site does", not ok("POST", "/settings/save", H, fetch_site="cross-site"))
check("Origin: null falls back to the Referer", ok("POST", "/x", H, origin="null", referer="https://dash.example.com/p"))
check("GETs are never blocked", ok("GET", "/status", H, origin="https://evil.example"))
check("machine endpoints (postback, trackers) are exempt", ok("POST", "/postback", H, origin="https://glitchy.example"))
check("ALLOWED_ORIGINS lets a second domain in", ok("POST", "/x", H, origin="https://other.example", allowed=["https://other.example"]))
check("a non-browser client (no Origin/Referer/Sec-Fetch) is left to the login check", ok("POST", "/x", H))
mn = read("app/main.py")
check("the check runs before the login gate, on every unsafe request (login CSRF too)",
      mn.index("sec.origin_ok(request.method") < mn.index("if not public:\n        import time as _time"))

# =======================================================================================
print("-- sessions, logout, PIN gate, audit --")
se = importlib.import_module("app.sessions")
check("device labels", se.device_label("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36") == "Chrome on macOS"
      and se.device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Version/17.0 Mobile/15E148 Safari/604.1") == "Safari on iPhone")
check("session ids are long and random", len(se.new_sid()) >= 40 and se.new_sid() != se.new_sid())
class Col:
    def __init__(s, n): s.n = n
    def __eq__(s, v): return lambda r: getattr(r, s.n) == v
    def __hash__(s): return 1
class Row(types.SimpleNamespace): pass
class Q:
    def __init__(s, rows): s.rows = rows
    def filter(s, *c): return Q([r for r in s.rows if all(f(r) for f in c)])
    def first(s): return s.rows[0] if s.rows else None
class SDB:
    def __init__(s, rows): s.rows = rows
    def query(s, m): return Q(s.rows)
    def commit(s): pass
    def rollback(s): pass
Row.sid = Col("sid")
M = types.SimpleNamespace(UserSession=Row)
from datetime import datetime, timedelta
live = Row(sid="S1", user_id=7, revoked_at=None, created_at=datetime.utcnow() - timedelta(hours=1), last_seen_at=datetime.utcnow() - timedelta(hours=1), ip="")
gone = Row(sid="S2", user_id=7, revoked_at=datetime.utcnow(), created_at=datetime.utcnow(), last_seen_at=datetime.utcnow(), ip="")
old = Row(sid="S3", user_id=7, revoked_at=None, created_at=datetime.utcnow() - timedelta(days=9), last_seen_at=datetime.utcnow(), ip="")
db = SDB([live, gone, old])
check("a live session passes and gets its last-seen stamped", se.check(db, M, "S1", 7, "1.2.3.4", 86400 * 7) is live and live.ip == "1.2.3.4")
check("a signed-out device, someone else's id, an unknown id, a too-old session: all refused",
      se.check(db, M, "S2", 7) is None and se.check(db, M, "S1", 8) is None and se.check(db, M, "nope", 7) is None
      and se.check(db, M, "S3", 7, max_age_s=86400 * 7) is None)
check("every request checks the cookie's session row; pre-v148 cookies are adopted, not logged out",
      "_sessions.check(d, _models, sid, user.id" in mn and 'request.session["sid"] = _sessions.create(d, _models, user' in mn)
check("…and the auth cache is keyed on it and cleared when a session row changes",
      "key = (uid, fp, sid)" in mn and '@_sa_event.listens_for(_models.UserSession, "after_update")' in mn)
au = read("app/routes/auth.py")
check("login mints a session row; logout is POST and ends this device's row; GET /logout only asks",
      'request.session["sid"] = sessions.create(db, models, user' in au and '@router.post("/logout")' in au
      and "sessions.revoke(db, models, sid" in au and 'render(request, "logout.html"' in au)
check("no GET links to /logout remain", 'href="/logout"' not in read("app/templates/base.html") + read("app/templates/login_2fa.html") + read("app/templates/login_2fa_setup.html"))
sp = read("app/routes/settings_page.py"); st = read("app/templates/settings.html")
check("Settings › Security lists signed-in devices with per-device sign-out (own sessions only)",
      '/settings/sessions/{row_id}/revoke' in sp and "row.user_id != me.id" in sp and "Signed-in devices" in st and "Sign out all others" in st)
check("'sign out everywhere' also ends every session row", "sessions.revoke_user(db, models, user.id" in read("app/users.py"))
sg = read("app/security_gate.py")
ns2 = {"config": types.SimpleNamespace(SECURITY_PIN="4321")}
exec(FUT + grab(sg, "pin_enabled") + grab(sg, "pin_needed") + grab(sg, "safe_next") + "\nPIN_PATHS = ('/cookies/', '/settings/users/')\n", ns2)
check("PIN gate: sensitive POSTs need it once per session", ns2["pin_needed"]("POST", "/cookies/save", False)
      and not ns2["pin_needed"]("POST", "/cookies/save", True) and not ns2["pin_needed"]("GET", "/cookies", False) and not ns2["pin_needed"]("POST", "/status/x", False))
sn = ns2["safe_next"]
check("the unlock redirect can't leave the site", sn("/settings") == "/settings" and sn("//evil.com") == "/" and sn("https://evil.com") == "/"
      and sn("/\\evil.com") == "/" and sn("/ok\r\nSet-Cookie: x") == "/")
check("the gate is wired into the middleware and the unlock route uses safe_next",
      "_gate.pin_needed(request.method, path" in mn and "security_gate.safe_next(next)" in read("app/routes/security.py"))
ad = importlib.import_module("app.audit")
check("audit: every POST/PUT/PATCH/DELETE by a signed-in user, minus UI chatter",
      ad.should_record("POST", "/status/pause", True) and not ad.should_record("GET", "/status", True)
      and not ad.should_record("POST", "/super-launcher/autosave", True) and not ad.should_record("POST", "/login", False))
check("…queued off the request (a busy database never slows a click)", "_audit.submit(_u, sec.client_ip(request)" in mn and "def _writer()" in read("app/audit.py"))
check("named entries for the sensitive events", all(x in (au + sp + read("app/routes/oauth.py") + read("app/routes/cookies_admin.py"))
      for x in ('"login"', '"logout"', '"2fa.disabled"', '"2fa.reset_by_admin"', '"session.revoked"', '"tiktok.connected"', '"cookies.saved"')))
check("the owner sees the audit trail", 'id="audit"' in st and "sec.audit" in st)
bg = read("app/background.py")
check("housekeeping prunes sessions, the audit trail and the app log (which grew forever)",
      "sessions.prune(db, models)" in bg and "audit.prune(db, models)" in bg and "models.AppLog.created_at <" in bg)
check("logging is configured (log.info used to go nowhere)", "_logging.basicConfig(" in mn)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
