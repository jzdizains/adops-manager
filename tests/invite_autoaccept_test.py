"""Auto-accept a TikTok BC invite end to end (v143).

Partners sends the invite → invite_mail reads the one-time link from the mailbox →
bc_invite_accept drives the browser to Join → membership is confirmed via the API. This
tests the two testable halves for real (the IMAP parser and the orchestration state
machine) with fakes, and source-asserts the browser/route/wiring that needs the app or
Playwright to run."""
import os, sys, types, tempfile, pathlib, importlib
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

# ---------------------------------------------------------------- invite_mail (real)
tmp = tempfile.mkdtemp()
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
_mod("app.config", DATA_DIR=pathlib.Path(tmp))
im = importlib.import_module("app.invite_mail")

print("-- invite mailbox: credentials on disk, never rendered back --")
im.save_config("imap.gmail.com", "me@gmail.com", "app-pw-123", port=993)
check("is_configured once saved", im.is_configured())
st = im.status()
check("status carries host/user but NOT the password", st["host"] == "imap.gmail.com" and st["user"] == "me@gmail.com" and "password" not in st)

def _msg(subject, code, minutes_ago=1):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "no-reply@tiktok.com"
    m["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    m.set_content("plain fallback")
    m.add_alternative(f'<a href="https://business.tiktok.com/?invite_code={code}">Join</a>', subtype="html")
    return m.as_bytes()

class FakeIMAP:
    def __init__(self, msgs): self.msgs = msgs
    def select(self, folder, readonly=False): return ("OK", [b"1"])
    def search(self, charset, *crit): return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self.msgs)))])
    def fetch(self, mid, spec): return ("OK", [(b"x", self.msgs[int(mid) - 1])])
    def logout(self): pass

im._connect = lambda c: FakeIMAP([
    _msg('You are invited to join the Business Center "U21 Elegant Muse LLR"', "c062fe678b6adc984096dc184c142d1d", 2),
    _msg('You are invited to join the Business Center "Other LLC"', "aa11bb22cc33dd44ee55ff6600112233", 5),
    _msg("A newsletter with no invite", "", 3),          # no code → ignored
])
res = im.find_invite_links(bc_name_hint="U21 Elegant", since_minutes=60, limit=5)
check("reads invite links from the mailbox", res["ok"] and len(res["invites"]) == 2, res)
check("extracts the one-time code and rebuilds the canonical URL",
      res["invites"][0]["invite_code"] == "c062fe678b6adc984096dc184c142d1d"
      and res["invites"][0]["url"].endswith("?invite_code=c062fe678b6adc984096dc184c142d1d"))
check("the BC-name hint sorts the matching invite to the front", "U21" in (res["invites"][0]["bc_name"] or ""))
old = im.find_invite_links(bc_name_hint="", since_minutes=1, limit=5)   # everything is 2–5 min old → outside a 1-min window
check("respects the time window (nothing within the last minute)", old["ok"] and old["invites"] == [], old)

# ---------------------------------------------------------------- orchestration state machine
# Everything the fakes do is driven by this mutable dict, so each scenario just sets it.
CTL = {"active": False, "configured": True,
       "invites": [{"invite_code": "deadbeef" * 4, "url": "https://business.tiktok.com/?invite_code=" + "deadbeef" * 4,
                    "subject": "join U21 Elegant Muse LLR", "bc_name": "U21 Elegant Muse LLR", "received": ""}],
       "accept": {"ok": True, "joined": True, "action": "joined", "detail": "clicked Join", "error": ""}}
STATE = CTL   # alias for readability below
_mail_stub = _mod("app.invite_mail",
                  is_configured=lambda: CTL["configured"],
                  find_invite_links=lambda bc_name_hint="", since_minutes=60, limit=5: {"ok": True, "invites": CTL["invites"]})
setattr(pkg, "invite_mail", _mail_stub)   # Part 1 imported the real module; rebind the package attr to the stub
def _accept(url, name, on_step=None, should_stop=None):
    if CTL["accept"].get("ok"):
        CTL["active"] = True                               # a successful Join grants access
    return CTL["accept"]
_mod("app.bc_invite_accept", accept_invite=_accept)
_mod("app.partners", find_member=lambda token, bc, email: ({"user_id": "u1", "relation_status": "CONFIRM"} if CTL["active"] else None))
_mod("app.queries", any_access_token=lambda db: "tok", get_setting=lambda db, k, d="": "", set_setting=lambda db, k, v: None)
_mod("app.models", utcnow=lambda: 0, InviteAccept=object)
_mod("app.jobs", enqueue=lambda *a, **k: types.SimpleNamespace(id=1))
_mod("sqlalchemy"); _mod("sqlalchemy.orm", Session=object)
aa = importlib.import_module("app.invite_autoaccept")

class DB:
    def commit(self): pass
def rec(**kw):
    d = dict(bc_id="765", bc_name="U21 Elegant Muse LLR", email="me@gmail.com", role="ADMIN",
             join_name="Admin", status=aa.WAITING, detail="", invite_code="", attempts=0, updated_at=None)
    d.update(kw)
    return types.SimpleNamespace(**d)

print("-- the happy path: link present → Join → API-confirmed → joined --")
CTL.update(active=False, configured=True)
r = rec(); aa.run(DB(), r)
check("with the invite present, one run drives Join and the API confirms → joined",
      r.status == aa.JOINED and "joined" in r.detail.lower(), r.status)

print("-- already a member (returning account restored on re-invite) → no browser needed --")
CTL["active"] = True
r = rec(); aa.run(DB(), r)
check("an account that already has access is marked already_member up front", r.status == aa.ALREADY)

print("-- no matching invite email yet → waits, not a failure --")
CTL.update(active=False, configured=True, invites=[])
r = rec(); aa.run(DB(), r)
check("no matching invite email yet → stays waiting (not a failure)", r.status == aa.WAITING, r.status)
r2 = rec(attempts=aa.MAX_ATTEMPTS); aa.run(DB(), r2)
check("after enough tries with no email → error, accept by hand", r2.status == aa.ERROR and "hand" in r2.detail.lower())

print("-- no mailbox at all → clean error --")
CTL.update(active=False, configured=False)
r = rec(); aa.run(DB(), r)
check("no mailbox configured → error telling the operator to accept by hand", r.status == aa.ERROR and "hand" in r.detail.lower())

print("-- a browser failure surfaces with the link so it can be done manually --")
CTL.update(active=False, configured=True,
           invites=[{"invite_code": "f" * 32, "url": "https://business.tiktok.com/?invite_code=" + "f" * 32,
                     "subject": "join U21 Elegant Muse LLR", "bc_name": "U21 Elegant Muse LLR", "received": ""}],
           accept={"ok": False, "error": "TikTok challenge", "joined": False, "action": ""})
r = rec(); aa.run(DB(), r)
check("accept failure → error carrying the reason and the link", r.status == aa.ERROR and "challenge" in r.detail.lower() and "invite_code=" in r.detail)

# ---------------------------------------------------------------- source / wiring
print("-- browser accept module --")
bia = read("app/bc_invite_accept.py")
check("shares the single-browser lock with the page builder (never two Chromiums at once)", "from .instant_page_builder import (" in bia and "_LOCK," in bia)
check("injects the session at .tiktok.com so it authenticates business.tiktok.com", '"domain": ".tiktok.com"' in bia)
check("only accepts a real invite link; leaves the post-Join 2FA alone", 'invite_code=' in bia and "2-step" in bia and "already_member" in bia)

print("-- orchestration wired into jobs + the sweep --")
check("registered as a slow-lane job", '@jobs.handler("invite_autoaccept")' in read("app/job_handlers.py") and '"invite_autoaccept"' in read("app/jobs.py").split("SLOW_KINDS = {")[1][:400])
check("advanced on the slow sweep, guarded so it never breaks it", "invite_autoaccept.poll(db)" in read("app/background.py"))
check("a durable per-run table exists", "class InviteAccept(Base)" in read("app/models.py"))

print("-- Partners page: toggle, mailbox, live feed, and it starts on invite --")
pp = read("app/routes/partners_page.py")
check("sending an invite starts auto-accept when it's on and the mailbox is set", "invite_autoaccept.enabled(db) and invite_mail.is_configured()" in pp and "invite_autoaccept.start(" in pp)
check("routes: toggle, save mailbox, test, live feed", all(x in pp for x in ('@router.post("/partners/autoaccept")', '@router.post("/partners/mailbox")', '@router.post("/partners/mailbox/test")', '@router.get("/partners/autoaccept.json")')))
tp = read("app/templates/partners.html")
check("UI: the toggle, the mailbox form, and a self-updating activity feed",
      'id="aaToggle"' in tp and 'action="/partners/mailbox"' in tp and 'id="aaFeed"' in tp and "/partners/autoaccept.json" in tp)
check("the password field is never pre-filled with the real value", 'name="password"' in tp and "unchanged" in tp)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
