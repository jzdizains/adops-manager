"""Partners page rebuilt as self-invite (v142).

The old "set up a new partner" flow (share ad accounts to a partner BC, per-account
member assignment, TikTok-account assignment) is replaced by the one move operators
actually need: invite an email into a Business Center as ADMIN — which gives that email
every ad account in the BC at once. It reuses the already-tested bc_assets.invite, and
the page shows the BC's live member list (pending → accepted) via /partners/members.json.

Route behaviour (fastapi/sqlalchemy stubbed) + template asserts."""
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

class _Router:
    def _deco(self, *a, **k):
        def wrap(fn): return fn
        return wrap
    get = _deco
    post = _deco

class Redirect:
    def __init__(self, url, status_code=303): self.url = url; self.status_code = status_code
class JSONResponse:
    def __init__(self, content, status_code=200): self.content = content; self.status_code = status_code

_mod("fastapi", APIRouter=_Router, Depends=_Any(), Form=_Any(), Request=_Any)
_mod("fastapi.responses", JSONResponse=JSONResponse, RedirectResponse=Redirect)
_mod("sqlalchemy.orm", Session=_Any)
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.models", BusinessCenter=object, InviteAccept=object); _mod("app.database", get_db=lambda: None); _mod("app.templating", render=lambda *a, **k: None)
_mod("app.balances", bc_portal_url=lambda b: "portal/" + str(b))
_mod("app.tiktok_api", BC_USER_ROLES=("ADMIN", "STANDARD"))

INVITES, SETTINGS = [], {}
def _invite(db, bc_id, email, role="ADMIN"):
    INVITES.append((bc_id, email, role))
    if bc_id == "BADBC":
        return {"error": "No stored token is Admin of that BC — send the first invite from its own login."}
    if email == "refuse@x.com":
        return {"steps": [{"ok": False, "error": "member limit reached"}]}
    return {"steps": [{"ok": True}], "summary": "invitation sent"}
def _members(db, bc_id):
    return {"members": [{"email": "you@co.com", "role": "ADMIN", "status": "PENDING"}], "bc_name": "BC " + bc_id}
_mod("app.bc_assets", invite=_invite, members=_members, _bcs_for_token=lambda t: {})
_mod("app.queries",
     any_access_token=lambda db: "tok",
     get_setting=lambda db, k, d="": SETTINGS.get(k, d),
     set_setting=lambda db, k, v: SETTINGS.update({k: v}))
AUTO = {"on": False, "started": []}
_mod("app.invite_autoaccept",
     enabled=lambda db: AUTO["on"], set_enabled=lambda db, on: AUTO.update(on=on),
     WAITING="waiting_email", ACCEPTING="accepting",
     start=lambda db, bid, name, email, jn, role="ADMIN": AUTO["started"].append((bid, email, role)))
_mod("app.invite_mail", is_configured=lambda: AUTO["on"],
     status=lambda: {"configured": AUTO["on"], "host": "", "user": "", "port": 993, "folder": "INBOX", "use_ssl": True, "saved_at": ""},
     save_config=lambda *a, **k: {"ok": True}, test_connection=lambda: {"ok": True, "detail": "ok"})

# A DB that answers the small lookups partners_page makes (BC name, accept rows) without a real ORM.
class _QDB:
    def query(self, *a, **k): return self
    def filter_by(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def all(self): return []
    def __iter__(self): return iter([])   # bc_name lookup: next((b.name for b in db.query(...).filter_by(...)), "")

import importlib
pp = importlib.import_module("app.routes.partners_page")

print("-- invite-admin sends one ADMIN invite through bc_assets.invite --")
INVITES.clear(); SETTINGS.clear()
r = pp.invite_admin(request=object(), bc_id="765 88-2 ", email="you@co.com", role="ADMIN", db=_QDB())
check("cleans the BC id to digits and invites the email as ADMIN", INVITES == [("765882", "you@co.com", "ADMIN")], INVITES)
check("remembers the email for next time", SETTINGS.get("partners_last_email") == "you@co.com")
check("success redirects back with an ok message + BC anchor", r.status_code == 303 and "ok=" in r.url and "#bc-765882" in r.url)

print("-- role is validated; bad role falls back to ADMIN --")
INVITES.clear()
pp.invite_admin(request=object(), bc_id="111", email="a@b.com", role="OWNER", db=_QDB())
check("an unknown role is coerced to ADMIN (never invents a role)", INVITES == [("111", "a@b.com", "ADMIN")], INVITES)

print("-- guards --")
INVITES.clear()
r_noat = None
import app.queries as _q
_orig = _q.any_access_token; _q.any_access_token = lambda db: None
r_noat = pp.invite_admin(request=object(), bc_id="111", email="a@b.com", role="ADMIN", db=_QDB())
_q.any_access_token = _orig
check("no TikTok connection → refused, nothing invited", "err=" in r_noat.url and INVITES == [])
r_bad = pp.invite_admin(request=object(), bc_id="111", email="not-an-email", role="ADMIN", db=_QDB())
check("a bad email is refused before any call", "err=" in r_bad.url and INVITES == [])
r_none = pp.invite_admin(request=object(), bc_id="", email="a@b.com", role="ADMIN", db=_QDB())
check("no BC is refused", "err=" in r_none.url)

print("-- TikTok refusal surfaces the reason --")
INVITES.clear()
r_ref = pp.invite_admin(request=object(), bc_id="111", email="refuse@x.com", role="ADMIN", db=_QDB())
check("a refused invite comes back as an error, not a false success", "err=" in r_ref.url)
r_badbc = pp.invite_admin(request=object(), bc_id="BADBC", email="a@b.com", role="ADMIN", db=_QDB())
check("bc_assets.invite's own error (not Admin of the BC) is surfaced", "err=" in r_badbc.url)

print("-- members.json returns the live member list --")
m = pp.partners_members(bc_id="765 882", db=object())
check("cleans the id and returns members incl. pending", m.content.get("members") and m.content["members"][0]["status"] == "PENDING")
m0 = pp.partners_members(bc_id="", db=object())
check("no BC id → clean error, no crash", m0.content.get("error") == "no BC")

print("-- template: the simple invite flow replaced the partner-sharing setup --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
t = read("app/templates/partners.html")
check("has the invite-as-Admin form posting to /partners/invite-admin",
      'action="/partners/invite-admin"' in t and 'name="bc_id"' in t and 'name="email"' in t and 'value="ADMIN"' in t)
check("loads the live member list from members.json", "/partners/members.json?bc_id=" in t and 'id="membersCard"' in t)
check("the old partner-sharing setup is gone",
      "/partners/create" not in t and "share_advertiser_ids" not in t and "tt_account_roles" not in t and "Set up a new partner" not in t)
src = read("app/routes/partners_page.py")
check("routes: invite-admin + members.json exist; the old create/assets/retry/tick are removed",
      '@router.post("/partners/invite-admin")' in src and '@router.get("/partners/members.json")' in src
      and '/partners/create' not in src and '/partners/assets' not in src)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
