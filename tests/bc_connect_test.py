"""Connecting a Business Center — the run must never claim more than TikTok confirms.

The bug this covers, in the operator's words: "it showed that it completed all the tasks
... And unfortunately nothing was indeed connected."

Two ways that happened, both tested here:
  1. the run was a PREVIEW (no mode reached the server, and preview was the default), so
     it read everything, listed every step and finished green having sent nothing;
  2. the run really sent /bc/partner/add/, TikTok answered ok — but a partnership only
     takes effect once the receiving Business Center approves it, so the ad accounts
     never became assets of the main BC and nothing could be linked to them.

Runs without fastapi/sqlalchemy installed: bc_assets is exercised against stub modules.
"""
import sys, types, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

# ---- stub sqlalchemy.orm.Session (bc_assets only uses it as a type hint) ----
if "sqlalchemy" not in sys.modules:
    sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm")
    class Session: pass
    orm.Session = Session; sa.orm = orm
    sys.modules["sqlalchemy"], sys.modules["sqlalchemy.orm"] = sa, orm

# ---- a package shell so `from . import models, queries, tiktok_api` resolves ----
pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
sys.modules["app"] = pkg

models = types.ModuleType("app.models")
class _Col:                    # AdAccount.access_token etc. are class attributes in a real
    def __eq__(self, o): return self       # model, and bc_assets builds filters out of them
    def __ne__(self, o): return self
class AdAccount:
    access_token = id = advertiser_name = enabled = _Col()
    def __init__(self, advertiser_id, advertiser_name, access_token, owner_bc_id, enabled=True):
        self.advertiser_id, self.advertiser_name = advertiser_id, advertiser_name
        self.access_token, self.owner_bc_id, self.enabled = access_token, owner_bc_id, enabled
class PixelRecord:
    def __init__(self, owner_bc_id): self.owner_bc_id = owner_bc_id
models.AdAccount, models.PixelRecord = AdAccount, PixelRecord
sys.modules["app.models"] = models

STORE, ACCOUNTS, PIXELS = {}, [], []
queries = types.ModuleType("app.queries")
queries.get_setting = lambda db, k, d="": STORE.get(k, d)
def _upsert(db, k, v): STORE[k] = v
queries.upsert_setting = _upsert
queries.set_setting = _upsert
queries.any_access_token = lambda db: "tok-main"
sys.modules["app.queries"] = queries

tiktok_api = types.ModuleType("app.tiktok_api")
class TikTokError(Exception):
    def __init__(self, message="boom", code=40000):
        super().__init__(message); self.message, self.code = message, code
tiktok_api.TikTokError = TikTokError
tiktok_api.ADVERTISER_ROLES = ("ADMIN", "OPERATOR", "ANALYST")
sys.modules["app.tiktok_api"] = tiktok_api

import importlib
bc_assets = importlib.import_module("app.bc_assets")

# ---- a fake Session: only the few query shapes bc_assets uses ----
class _Q:
    def __init__(self, rows): self.rows = list(rows)
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def all(self): return list(self.rows)
    def first(self): return self.rows[0] if self.rows else None
class DB:
    def query(self, model):
        return _Q(ACCOUNTS if model is AdAccount else PIXELS)
    def commit(self): pass
    def rollback(self): pass
db = DB()

MAIN, SAT = "111", "222"
ACCOUNTS[:] = [AdAccount("A1", "acct one", "tok-main", MAIN),
               AdAccount("A2", "acct two", "tok-sat", SAT)]
PIXELS[:] = [PixelRecord(MAIN)]
STORE["main_bc_id"] = MAIN

BCS = {"tok-main": [{"bc_info": {"bc_id": MAIN, "name": "Main BC"}, "user_role": "ADMIN"}],
       "tok-sat": [{"bc_info": {"bc_id": SAT, "name": "Satellite BC"}, "user_role": "ADMIN"}]}

world = {"shared": False, "writes": [], "partner_added": []}

def reset(shared):
    world["shared"], world["writes"], world["partner_added"] = shared, [], []
    PIXEL_LINKS["page2"] = [{"advertiser_id": "A1"}]      # back to "A2 not linked yet"
    TT_LINKS.clear(); TT_LINKS["TT1"] = ["A1"]

tiktok_api.list_business_centers = lambda tok: BCS.get(tok, [])

def _assets(tok, bc_id, kind, max_pages=20):
    if bc_id == MAIN:
        if kind == "PIXEL":      return [{"asset_id": "PX1", "pixel_code": "CODE1", "asset_name": "Main pixel"}]
        if kind == "TT_ACCOUNT": return [{"asset_id": "TT1", "tt_asset_handle": "@one"}]
        # A partner-shared ad account is NOT an asset the main BC owns — it only ever shows
        # up under /bc/partner/asset/get/. Modelling that is the point of this stub.
        if kind == "ADVERTISER": return [{"asset_id": "A1"}]
    if bc_id == SAT and kind == "ADVERTISER":
        return [{"asset_id": "A2"}]
    return []
tiktok_api.bc_assets_admin = _assets
# a pixel linked to more ad accounts than fit on one page: A2 lands on page 2, which is
# exactly where a single-page reader stopped looking
PIXEL_LINKS = {"page1": [{"advertiser_id": "P%d" % i} for i in range(50)],
               "page2": [{"advertiser_id": "A1"}]}
def _pixel_link_get(tok, bc, code, page=1, page_size=50):
    if page == 1:
        return {"list": PIXEL_LINKS["page1"], "page_info": {"total_page": 2}}
    return {"list": PIXEL_LINKS["page2"], "page_info": {"total_page": 2}}
tiktok_api.bc_pixel_link_get = _pixel_link_get
def _pixel_linked(tok, bc, code, max_pages=40):
    out = []
    for pg in range(1, max_pages + 1):
        d = _pixel_link_get(tok, bc, code, page=pg)
        out += [x["advertiser_id"] for x in d["list"]]
        if pg >= d["page_info"]["total_page"]:
            break
    return out
tiktok_api.bc_pixel_linked_advertisers = _pixel_linked
TT_LINKS = {"TT1": ["A1"]}
tiktok_api.bc_tt_account_advertisers = (lambda tok, bc, aid, asset_type="TT_ACCOUNT", max_pages=20:
                                        list(TT_LINKS.get(aid, [])))
tiktok_api.bc_partner_asset_get = (lambda tok, bc, partner, asset_type="ADVERTISER", share_type="SHARED_TO_ME":
                                   [{"asset_id": "A2"}] if world["shared"] else [])
tiktok_api.bc_partner_list = lambda tok, bc: ([{"partner_id": SAT}] if world["shared"] else [])

def _partner_add(tok, bc_id, partner_id, ids, role):
    world["partner_added"].append((bc_id, partner_id, list(ids), role))
    return {"code": 0}          # TikTok says ok whether or not the partnership is approved
tiktok_api.bc_partner_add = _partner_add
def _pixel_link(tok, bc, code, advs, rel="LINK"):
    world["writes"].append(("pixel", code, list(advs)))
    for a in advs:                       # the link really is made — page 2 of the read shows it
        PIXEL_LINKS["page2"].append({"advertiser_id": a})
    return {}
def _tt_link(tok, bc, asset_id, adv, asset_type="TT_ACCOUNT"):
    world["writes"].append(("profile", asset_id, adv))
    TT_LINKS.setdefault(asset_id, []).append(adv)
    return {}
tiktok_api.bc_pixel_link_update = _pixel_link
tiktok_api.bc_tt_account_link = _tt_link

# =========================================================================
print("\n-- the mode a button sends is never guessed --")
check("empty mode is refused", bc_assets.parse_mode("") is None)
check("missing mode is refused", bc_assets.parse_mode(None) is None)
check("a typo is refused", bc_assets.parse_mode("sendd") is None)
check("preview is preview", bc_assets.parse_mode("preview") == "preview")
check("send is send", bc_assets.parse_mode(" SEND ") == "send")

print("\n-- a preview sends nothing and says so --")
reset(shared=False)
bc_assets.scan(db)
rep = bc_assets.connect_bc(db, SAT, dry_run=True)
check("no partner_add", not world["partner_added"], str(world["partner_added"]))
check("no pixel/profile writes", not world["writes"], str(world["writes"]))
check("summary leads with PREVIEW", rep.get("summary", "").startswith("PREVIEW ONLY"), rep.get("summary"))
check("no step claims success", all(s["ok"] is not True or not s.get("request")
                                    for s in rep["steps"]), str(rep["steps"])[:200])

print("\n-- a real run that TikTok has not approved yet: no false success --")
reset(shared=False)
rep = bc_assets.connect_bc(db, SAT, dry_run=False)
check("the share was actually sent", len(world["partner_added"]) == 1, str(world["partner_added"]))
check("nothing was linked to an account the main BC can't see", not world["writes"], str(world["writes"]))
check("the report flags the approval", rep.get("approval_needed") is True, str(rep.get("summary")))
check("A2 is listed as waiting", rep.get("waiting") == ["A2"], str(rep.get("waiting")))
check("summary says waiting for approval", "waiting for the partnership" in rep.get("summary", ""), rep.get("summary"))
check("the run is not reported green", any(s["ok"] is False for s in rep["steps"]))

print("\n-- a real run the main BC can see: it links, and only claims what is confirmed --")
reset(shared=True)
bc_assets.scan(db)
rep = bc_assets.connect_bc(db, SAT, dry_run=False)
check("pixel linked to A2", ("pixel", "CODE1", ["A2"]) in world["writes"], str(world["writes"]))
check("profile linked to A2", ("profile", "TT1", "A2") in world["writes"], str(world["writes"]))
check("no approval banner", not rep.get("approval_needed"), rep.get("summary"))
check("summary says Sent", rep.get("summary", "").startswith("Sent"), rep.get("summary"))
check("summary quotes what TikTok confirms", "TikTok now confirms" in rep.get("summary", ""), rep.get("summary"))

print("\n-- when TikTok won't answer the check, don't skip the work --")
reset(shared=False)
_partner_get = tiktok_api.bc_partner_asset_get
def _refuse(*a, **k):
    raise TikTokError("not supported here", 40002)
tiktok_api.bc_partner_asset_get = _refuse
bc_assets.scan(db)                     # audit taken while that read is refusing
rep = bc_assets.connect_bc(db, SAT, dry_run=False)
tiktok_api.bc_partner_asset_get = _partner_get
check("the links are still attempted", any(w[0] == "pixel" for w in world["writes"]), str(world["writes"]))
check("nothing is called waiting on a failed read", not rep.get("waiting"), str(rep.get("waiting")))
check("the failed read is reported", any("partner share" in n for n in rep.get("notes", [])),
      str(rep.get("notes")))

print("\n-- the audit counts a partner-shared account as in the main BC --")
reset(shared=True)
snap = bc_assets.scan(db)
row = next(r for r in snap["accounts"] if r["advertiser_id"] == "A2")
check("A2 counts as in the main BC", row["in_main_bc"] is True, str(row))
check("and says it came in through a partner", row.get("shared_via") == SAT, str(row.get("shared_via")))
check("and the main BC does not own it — only the partner read finds it",
      "A2" not in {a["asset_id"] for a in _assets(None, MAIN, "ADVERTISER")})

print("\n-- a pixel link past the first page is still seen --")
reset(shared=True)
bc_assets.scan(db)
rep = bc_assets.connect_bc(db, SAT, dry_run=False)
row = next(r for r in bc_assets.snapshot(db)["accounts"] if r["advertiser_id"] == "A2")
check("the new pixel link is found on page 2", row["pixels"] == ["Main pixel"], str(row["pixels"]))
check("A2 is confirmed fully wired", rep.get("confirmed") == ["A2"], str(rep.get("confirmed")))
check("the summary confirms 1 of 1", "confirms 1 of 1" in rep.get("summary", ""), rep.get("summary"))

print("\n-- an unconfirmed account says WHICH check came back empty --")
reset(shared=True)
bc_assets.scan(db)
_tt = tiktok_api.bc_tt_account_link
tiktok_api.bc_tt_account_link = lambda *a, **k: {}      # TikTok says ok, nothing changes
rep = bc_assets.connect_bc(db, SAT, dry_run=False)
tiktok_api.bc_tt_account_link = _tt
check("it is not counted as wired", rep.get("confirmed") == [], str(rep.get("confirmed")))
check("the summary names the gap", "not every profile came back" in rep.get("summary", ""), rep.get("summary"))
check("the breakdown is per account", any(c["advertiser_id"] == "A2" and c["ok"] is False
                                          for c in rep.get("checks", [])), str(rep.get("checks")))

print("\n-- the page's poll never parses the whole snapshot --")
check("snapshot_at is the stored timestamp",
      bc_assets.snapshot_at(db) == bc_assets.snapshot(db).get("at"),
      bc_assets.snapshot_at(db))

print()
print(("FAILED: " + ", ".join(fails)) if fails else "all good")
sys.exit(1 if fails else 0)
