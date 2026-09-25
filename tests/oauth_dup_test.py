"""v155.23 — connecting a TikTok login that lists the same ad account twice (in two Business
Centers, or twice on one) crashed the callback with UNIQUE constraint failed: the session doesn't
autoflush, so the second listing didn't see the row just added and inserted it again. Now one
row per account, and a sync failure after a successful login shows a message instead of a 500."""
import os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m; return m
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
rp = _mod("app.routes"); rp.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("sqlalchemy.orm", Session=object)
class _Dep:
    def __call__(self, *a, **k): return None
fa = _mod("fastapi", APIRouter=lambda *a, **k: types.SimpleNamespace(get=lambda *a, **k: (lambda f: f), post=lambda *a, **k: (lambda f: f)),
          Depends=lambda f: None, Form=lambda *a, **k: None, Request=object)
_mod("fastapi.responses", RedirectResponse=object)
_mod("app.config", TIKTOK_APP_ID="", TIKTOK_SECRET="", APP_BASE_URL="")
_mod("app.database", get_db=lambda: None)
_mod("app.templating", render=lambda *a, **k: None)
settings = {}
def _distinct_tokens(db):
    seen = {}
    for a in db.rows["AdAccount"]:
        if a.access_token: seen.setdefault(a.access_token, a)
    return list(seen.items())
_mod("app.queries", set_setting=lambda db, k, v: settings.__setitem__(k, v), get_setting=lambda db, k, d="": settings.get(k, d), log=lambda *a, **k: None,
     distinct_tokens=_distinct_tokens)

class TikTokError(Exception):
    def __init__(self, code, message=""): self.code, self.message = code, message
listed = [{"asset_id": "7658134110610702356", "asset_name": "blue bat A"}, {"asset_id": "7658134110610702356", "asset_name": "blue bat A"}, {"asset_id": "999", "asset_name": "other"}]
api = _mod("app.tiktok_api", TikTokError=TikTokError,
           list_business_centers=lambda tok: [{"bc_id": "bc1", "name": "BC 1"}, {"bc_id": "bc2", "name": "BC 2"}],
           get_bc_balance=lambda tok, bc: {}, parse_bc_balance=lambda d: (0.0, "USD"),
           list_bc_advertisers=lambda tok, bc, page=1: {"list": listed, "page_info": {"total_page": 1}},
           get_authorized_advertisers=lambda tok: [], get_advertiser_info=lambda tok, ids: [])

class Row:
    def __init__(self, **kw):
        self.__dict__.update(dict(advertiser_id="", advertiser_name="", access_token="", refresh_token="", status="", enabled=True,
                                  owner_bc_id="", owner_user_id=None, bc_id="", name="", balance=0.0, currency="", timezone=""))
        self.__dict__.update(kw)
class Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a): return self
    def filter_by(self, **kw): return Q([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return list(self.rows)
    def __iter__(self): return iter(self.rows)
class DB:
    """No autoflush: a query never sees a row added since the last commit — like the real one."""
    def __init__(self): self.rows = {"AdAccount": [], "BusinessCenter": [], "AccountAccess": []}; self.pending = []; self.commits = 0
    def query(self, model):
        if isinstance(model, _Col):                      # column query (retired BC ids): tuples, filtered by the flag
            return Q([(r.bc_id,) for r in self.rows["BusinessCenter"] if r.__dict__.get("retired", False)])
        return Q(self.rows[model.__name__])
    def add(self, r): self.pending.append(r)
    def commit(self):
        for r in self.pending:
            key = type(r).__name__ if type(r).__name__ in self.rows else ("AdAccount" if hasattr(r, "advertiser_id") and r.advertiser_id else "BusinessCenter")
            if key == "AdAccount" and any(x.advertiser_id == r.advertiser_id for x in self.rows["AdAccount"]):
                raise RuntimeError("UNIQUE constraint failed: ad_accounts.advertiser_id")
            self.rows[key].append(r)
        self.pending = []; self.commits += 1
    def rollback(self): self.pending = []
    def delete(self, r):
        for k in self.rows: 
            if r in self.rows[k]: self.rows[k].remove(r)
class _Col:
    def __init__(self, n): self.n = n
    def __eq__(self, v): return ("eq", self.n, v)
    __hash__ = object.__hash__
models = _mod("app.models", AdAccount=type("AdAccount", (Row,), {}), BusinessCenter=type("BusinessCenter", (Row,), {"bc_id": _Col("bc_id"), "retired": _Col("retired")}),
              AccountAccess=type("AccountAccess", (Row,), {"user_id": _Col("user_id"), "advertiser_id": _Col("advertiser_id")}),
              TikTokLogin=type("TikTokLogin", (Row,), {"access_token": _Col("access_token"), "core_user_id": _Col("core_user_id"), "user_id": _Col("user_id")}))

import importlib
oauth = importlib.import_module("app.routes.oauth")
db = DB()
r = oauth.sync_accounts(db, "tok", user_id=5)
accts = db.rows["AdAccount"]
check("the same account listed under two BCs (and twice on one) becomes ONE row — no crash",
      sorted(a.advertiser_id for a in accts) == ["7658134110610702356", "999"], [a.advertiser_id for a in accts])
check("…owned by the connecting user, with the login's token", all(a.owner_user_id == 5 and a.access_token == "tok" for a in accts))
check("…the sync still reports what it saw", r["count"] >= 2)
# a second connect by another user: existing rows keep their owner
db2 = DB(); db2.rows["AdAccount"].append(models.AdAccount(advertiser_id="999", owner_user_id=1, access_token="old"))
oauth.sync_accounts(db2, "tok2", user_id=5)
check("an account another user already owns keeps its owner", [a.owner_user_id for a in db2.rows["AdAccount"] if a.advertiser_id == "999"] == [1] and len(db2.rows["AdAccount"]) == 2)
check("v155.38: …and the second user gets an access row for it (their workspace shows it too), stamped with their token",
      [(r.advertiser_id, r.user_id, r.access_token) for r in db2.rows["AccountAccess"]] == [("999", 5, "tok2")], str([r.__dict__ for r in db2.rows["AccountAccess"]]))
oauth.sync_accounts(db2, "tok2", user_id=5)
check("…a second sync reuses the row, never duplicates it", len(db2.rows["AccountAccess"]) == 1)
api.list_bc_advertisers = lambda tok, bc, page=1: {"list": [x for x in listed if x["asset_id"] != "999"], "page_info": {"total_page": 1}}
oauth.sync_accounts(db2, "tok2", user_id=5)
check("…and when that login stops listing it, the access row goes; the owner's account is untouched — still enabled, still the owner's token",
      db2.rows["AccountAccess"] == [] and [(a.owner_user_id, a.enabled, a.status, a.access_token) for a in db2.rows["AdAccount"] if a.advertiser_id == "999"] == [(1, True, "", "old")],
      str([a.__dict__ for a in db2.rows["AdAccount"] if a.advertiser_id == "999"]))
api.list_bc_advertisers = lambda tok, bc, page=1: {"list": listed, "page_info": {"total_page": 1}}
db4 = DB(); db4.rows["BusinessCenter"].append(models.BusinessCenter(bc_id="bc2", retired=True))
db4.rows["AdAccount"].append(models.AdAccount(advertiser_id="999", owner_user_id=1, access_token="old", status="ACCESS_LOST", enabled=False, owner_bc_id="bc2"))
api.list_bc_advertisers = lambda tok, bc, page=1: {"list": [x for x in listed if (x["asset_id"] == "999") == (bc == "bc2")], "page_info": {"total_page": 1}}
oauth.sync_accounts(db4, "tok", user_id=1)
check("v155.27: an account under a removed Business Center is never switched back on by a sync", [a.enabled for a in db4.rows["AdAccount"] if a.advertiser_id == "999"] == [False])
print("-- v155.29: one user, two TikTok logins — a sync of one never retires the other's accounts --")
db5 = DB()
db5.rows["AdAccount"] += [models.AdAccount(advertiser_id="OLD1", owner_user_id=1, access_token="tokA", enabled=True),
                          models.AdAccount(advertiser_id="OLD2", owner_user_id=1, access_token="tokA", enabled=True),
                          models.AdAccount(advertiser_id="GONE", owner_user_id=1, access_token="tokB", enabled=True)]
db5.rows["BusinessCenter"] += [models.BusinessCenter(bc_id="bcA", access_token="tokA", owner_user_id=1), models.BusinessCenter(bc_id="bcB", access_token="tokB", owner_user_id=1)]
api.list_business_centers = lambda tok: [{"bc_id": "bcB", "name": "BC B"}]
api.list_bc_advertisers = lambda tok, bc, page=1: {"list": [{"asset_id": "NEW1", "asset_name": "new"}], "page_info": {"total_page": 1}}
oauth.sync_accounts(db5, "tokB", user_id=1)
st = {a.advertiser_id: (a.enabled, a.status) for a in db5.rows["AdAccount"]}
check("login B's sync: its own missing account is retired, login A's accounts are left alone, the new one is added",
      st["GONE"] == (False, "ACCESS_LOST") and st["OLD1"] == (True, "") and st["OLD2"] == (True, "") and st["NEW1"][0] is True, str(st))
bst = {b.bc_id: b.status for b in db5.rows["BusinessCenter"]}
check("…and login A's Business Center keeps its status", bst["bcA"] == "" and bst["bcB"] == "", str(bst))
print("-- v155.39: every login syncs as the user who connected it (the registry), never as a sample account's owner --")
oa2 = read("app/routes/oauth.py"); q2 = read("app/queries.py"); bal = read("app/balances.py")
check("Connect TikTok / manual connect register the login (who + which dashboard user) before syncing",
      oa2.count("_register(db, access_token, refresh_token,") == 2 and "def register_login(" in q2 and 'core_user_id == core' in q2)
check("Sync all and the background re-sync walk the registry with each login's OWN user",
      "queries.logins(db, None if sc.everything else sc.user_id)" in oa2 and "user_id=lg.user_id" in oa2 and "for lg in queries.logins(db):" in bal and "user_id=lg.user_id" in bal
      and "user_id=acct.owner_user_id" not in oa2 and "user_id=acct.owner_user_id" not in bal)
check("a token found only on account rows is registered with the old guess, once — nothing is lost", "for tok, acct in distinct_tokens(db):" in q2 and "if tok not in known:" in q2)
check("Refresh TikTok token touches only THAT login's rows (it used to restamp every account with one token)",
      "filter(models.AdAccount.access_token == old)" in oa2 and "for row in db.query(models.AdAccount).all():\n        row.access_token = access_token" not in oa2)
check("the Accounts page shows the view's connected logins with the last sync's result", "_logins_for_page(db, sc, people)" in read("app/routes/dashboard.py") and "Connected TikTok login" in read("app/templates/accounts.html"))
src = read("app/routes/oauth.py")
check("the OAuth callback shows a message (and rolls back) if the sync fails after a successful login, instead of a 500",
      "except Exception as e:  # noqa: BLE001 — the login worked" in src and "db.rollback()" in src.split("def sync_accounts")[0])
check("the enrich pass asks TikTok once per account", 'ids = list(dict.fromkeys(a["advertiser_id"] for a in advertisers if a["advertiser_id"]))' in src)
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
