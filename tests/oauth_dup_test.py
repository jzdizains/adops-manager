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
_mod("app.queries", set_setting=lambda db, k, v: settings.__setitem__(k, v), get_setting=lambda db, k, d="": settings.get(k, d), log=lambda *a, **k: None)

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
    def filter_by(self, **kw): return Q([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return list(self.rows)
    def __iter__(self): return iter(self.rows)
class DB:
    """No autoflush: a query never sees a row added since the last commit — like the real one."""
    def __init__(self): self.rows = {"AdAccount": [], "BusinessCenter": []}; self.pending = []; self.commits = 0
    def query(self, model): return Q(self.rows[model.__name__])
    def add(self, r): self.pending.append(r)
    def commit(self):
        for r in self.pending:
            key = "AdAccount" if hasattr(r, "advertiser_id") and r.advertiser_id else "BusinessCenter"
            if key == "AdAccount" and any(x.advertiser_id == r.advertiser_id for x in self.rows["AdAccount"]):
                raise RuntimeError("UNIQUE constraint failed: ad_accounts.advertiser_id")
            self.rows[key].append(r)
        self.pending = []; self.commits += 1
    def rollback(self): self.pending = []
models = _mod("app.models", AdAccount=type("AdAccount", (Row,), {}), BusinessCenter=type("BusinessCenter", (Row,), {}))

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
src = read("app/routes/oauth.py")
check("the OAuth callback shows a message (and rolls back) if the sync fails after a successful login, instead of a 500",
      "except Exception as e:  # noqa: BLE001 — the login worked" in src and "db.rollback()" in src.split("def sync_accounts")[0])
check("the enrich pass asks TikTok once per account", 'ids = list(dict.fromkeys(a["advertiser_id"] for a in advertisers if a["advertiser_id"]))' in src)
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
