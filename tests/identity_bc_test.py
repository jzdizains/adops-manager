"""The Business Center sent with an ad's identity.

The failure this covers, verbatim from TikTok:

    code=40002 "You no longer have access to the TikTok account used in this ad.
    To continue editing, select a new identity and creative material or re-apply for access."

Nothing was disconnected. `identity_authorized_bc_id` must name the Business Center that
AUTHORIZED THE PROFILE — and in the shared-asset setup the profiles are assets of the MAIN
Business Center while the ad account is owned by a satellite. Sending the account's owner BC
asks a Business Center about a profile it has never had, and TikTok answers exactly as above.

Runs without fastapi/sqlalchemy: the resolver is exercised against stub modules.
"""
import sys, types, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

MAIN, SAT = "7658285881817202708", "7676193627814510609"

# ---- the identity resolver, with only the module graph it needs -------------------------
if "sqlalchemy" not in sys.modules:
    sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm")
    class Session: pass
    orm.Session = Session; sa.orm = orm
    sys.modules["sqlalchemy"], sys.modules["sqlalchemy.orm"] = sa, orm

pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
sys.modules["app"] = pkg
models = types.ModuleType("app.models")
class AdAccount:
    def __init__(self, advertiser_id, owner_bc_id, access_token="tok", advertiser_name=""):
        self.advertiser_id, self.owner_bc_id, self.access_token = advertiser_id, owner_bc_id, access_token
        self.advertiser_name = advertiser_name
models.AdAccount = AdAccount
sys.modules["app.models"] = models
tiktok_api = types.ModuleType("app.tiktok_api")
class TikTokError(Exception):
    def __init__(self, message="boom", code=40002):
        super().__init__(message); self.message, self.code = message, code
tiktok_api.TikTokError = TikTokError
sys.modules["app.tiktok_api"] = tiktok_api
bc_assets = types.ModuleType("app.bc_assets")
bc_assets.main_bc_id = lambda db: MAIN
sys.modules["app.bc_assets"] = bc_assets

# import only the helpers, not the whole 1500-line route module
import importlib.util
src = open(os.path.join(ROOT, "app", "routes", "campaigns.py")).read()
start = src.index("def _bc_candidates(")
end = src.index("def _identity_lists_item(")
helpers = types.ModuleType("app.routes.campaigns")
# the real module resolves `from .. import bc_assets` against its package — give the
# extracted helpers the same package context so the tested code path is the shipped one
helpers.__package__ = "app.routes"
helpers.__dict__.update({"models": models, "tiktok_api": tiktok_api, "Session": object,
                         "__package__": "app.routes", "__name__": "app.routes.campaigns"})
routes_pkg = types.ModuleType("app.routes"); routes_pkg.__path__ = [os.path.join(ROOT, "app", "routes")]
sys.modules["app.routes"] = routes_pkg
exec(compile(src[start:end], "campaigns-helpers", "exec"), helpers.__dict__)

acct = AdAccount("7676193585229758481", SAT)
asked = []
PROFILES = [{"identity_id": "TT_VANESS"}, {"identity_id": "TT_EMMA"}]
def fake_list(token, adv, identity_type=None, identity_authorized_bc_id=""):
    asked.append((identity_type, identity_authorized_bc_id))
    if identity_type != "BC_AUTH_TT":
        return []                                   # no TT_USER identities on this account
    if identity_authorized_bc_id == MAIN:
        return [dict(p) for p in PROFILES]          # the profiles live in the MAIN BC
    raise TikTokError("You no longer have access to the TikTok account used in this ad.", 40002)
tiktok_api.list_identities = fake_list

print("\n-- both Business Centers are asked, not just the account's owner --")
found = helpers._account_identities(acct, db=object())
bcs_asked = [bc for t, bc in asked if t == "BC_AUTH_TT"]
check("the owner BC is tried", SAT in bcs_asked, str(bcs_asked))
check("the main BC is tried too", MAIN in bcs_asked, str(bcs_asked))
check("the profiles are found", sorted(i["identity_id"] for i in found) == ["TT_EMMA", "TT_VANESS"],
      str(found))

print("\n-- each identity carries the BC it actually answered under --")
check("stamped with the main BC", all(i.get("_bc") == MAIN for i in found), str(found))
check("and that is what would be sent",
      all(helpers._bc_of(acct, i) == MAIN for i in found),
      str([helpers._bc_of(acct, i) for i in found]))
check("NOT the account's owner BC — the bug being fixed",
      all(helpers._bc_of(acct, i) != SAT for i in found))

print("\n-- an account owned by the main BC is unaffected --")
own = AdAccount("111", MAIN)
check("only one BC to try", helpers._bc_candidates(object(), own) == [MAIN],
      str(helpers._bc_candidates(object(), own)))

print("\n-- a non-BC identity never gets a BC id --")
check("TT_USER sends nothing", helpers._bc_of(acct, {"identity_type": "TT_USER", "_bc": MAIN}) == "")
check("AUTH_CODE sends nothing", helpers._bc_of(acct, {"identity_type": "AUTH_CODE"}) == "")

print("\n-- with no main BC recorded, behaviour is the old one, not a crash --")
bc_assets.main_bc_id = lambda db: ""
check("falls back to the owner BC", helpers._bc_candidates(object(), acct) == [SAT],
      str(helpers._bc_candidates(object(), acct)))
bc_assets.main_bc_id = lambda db: MAIN
check("no db at all is survived", helpers._bc_candidates(None, acct) == [SAT],
      str(helpers._bc_candidates(None, acct)))

print("\n-- an identity found under no BC still falls back rather than sending nothing --")
check("falls back to the owner BC",
      helpers._bc_of(acct, {"identity_type": "BC_AUTH_TT"}) == SAT)

# ---- and the error must stop reading as a broken connection ----------------------------
print("\n-- the carousel path sends the identity's BC too, not the account's owner --")
sys.modules["app.routes"] = routes_pkg
start2 = src.index("def resolve_account_identity(")
end2 = src.index("def _resolve_cover(")
class ConfigError(Exception): pass
helpers.__dict__["ConfigError"] = ConfigError
exec(compile(src[start2:end2], "campaigns-identity", "exec"), helpers.__dict__)

got = helpers.resolve_account_identity(object(), acct)
check("it picks the BC-authorized profile", got["identity_type"] == "BC_AUTH_TT", str(got))
check("and sends the MAIN BC with it", got.get("identity_authorized_bc_id") == MAIN, str(got))
check("never the ad account's owner BC", got.get("identity_authorized_bc_id") != SAT, str(got))

print("\n-- with no identity at all it refuses clearly rather than sending a broken ad --")
_orig = tiktok_api.list_identities
tiktok_api.list_identities = lambda *a, **k: []
try:
    helpers.resolve_account_identity(object(), acct)
    check("it raises", False, "no error raised")
except ConfigError as e:
    check("it raises a readable config error", "no TikTok" in str(e), str(e)[:80])
except Exception as e:
    check("it raises a readable config error", False, repr(e))
tiktok_api.list_identities = _orig

print("\n-- the error is explained as an identity problem, not a dead connection --")
for m in list(sys.modules):
    if m.startswith("app.error_messages"):
        del sys.modules[m]
import importlib
em = importlib.import_module("app.error_messages")
RAW = ("You no longer have access to the TikTok account used in this ad. To continue editing, "
       "select a new identity and creative material or re-apply for access.")
ex = em.explain("40002", RAW)
check("it is not called a permission problem", ex["is_permission"] is False, str(ex["is_permission"]))
check("the friendly text names the profile", "profile" in ex["friendly"].lower(), ex["friendly"])
check("and says nothing needs reconnecting", "reconnect" in ex["action"].lower(), ex["action"][:80])

class Log:
    error_code = "40002"
    error_message = ex["friendly"]
    error_technical = RAW
    advertiser_id = "7676193585229758481"
    spark_code_id = None
    template_id = None
act = em.fix_for(Log())
check("the button is no longer Reconnect", act and act["label"] != "Reconnect the account", str(act))
check("it points at Assets", act and "bc-assets" in act["href"], str(act))

print()
print(("FAILED: " + ", ".join(fails)) if fails else "all good")
sys.exit(1 if fails else 0)
