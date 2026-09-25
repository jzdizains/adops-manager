"""Per-user workspaces (v116): who sees what, and nothing leaks.

Functional (scope.py with stubbed models/users/config):
- a buyer always gets their own workspace, whatever the cookie says
- the super admin: Everyone by default, any user's view via the cookie, an inactive or
  unknown user falls back to Everyone
- allows / owns / owned / owner_for_new / filter_ids
- switch options (Everyone, Mine, then the others), claim() only for unowned accounts
- backfill() gives owner-less rows to the super admin
Static (the shipped files):
- every model in OWNED_MODELS has an owner column with the request-context default
- every route whose path names an account carries the account guard; every
  /creatives/{creative_id} route the creative guard
- each scoped page reads the view; the bell cache is keyed by view
- OAuth: new accounts / BCs land in the connecting user's workspace and retiring is
  confined to that login; balances use per-BC tokens
- launches carry the launching user; library picks are owner-scoped; auto-pick too
- P&L, Home, Campaigns, Audience, Inbox, Health, Team, Assistant all scoped
Runs without any dependency."""
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

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
_mod("sqlalchemy.orm", Session=object)
_mod("app.config", OWNER_EMAIL="janis@glitchy.ai")

class Col:
    def __init__(self, name): self.name = name
    def __eq__(self, o): return ("eq", self.name, o)
    def is_(self, o): return ("is", self.name, o)
    __hash__ = object.__hash__
class User:
    id = Col("id"); email = Col("email"); active = Col("active")
    def __init__(self, id, email, active=True):
        self.id, self.email, self.active = id, email, active
class AdAccount:
    advertiser_id = Col("advertiser_id"); owner_user_id = Col("owner_user_id"); owner_bc_id = Col("owner_bc_id")
    def __init__(self, adv, owner=None, bc=""): self.advertiser_id, self.owner_user_id, self.owner_bc_id = adv, owner, bc
class Owned:
    owner_user_id = Col("owner_user_id")
    def __init__(self, owner=None): self.owner_user_id = owner
models = _mod("app.models", User=User, AdAccount=AdAccount,
              **{n: type(n, (Owned,), {"owner_user_id": Col("owner_user_id")}) for n in ("Template", "Creative", "DisplayCard", "AdText", "SparkCode", "SparkCodeGroup", "Tag", "BusinessCenter", "PageTemplate")})
_mod("app.users", is_owner=lambda u: bool(u) and u.email == "janis@glitchy.ai", norm_email=lambda e: (e or "").strip().lower(),
     viewable_ids=lambda u: set(getattr(u, "can_view", ()) or ()))

import importlib
scope = importlib.import_module("app.scope")

# ---- a tiny fake db -----------------------------------------------------------------------
class Q:
    def __init__(self, rows, cols=None): self.rows = list(rows); self.cols = cols
    def filter(self, *conds):
        rows = self.rows
        for c in conds:
            if isinstance(c, tuple) and c[0] == "eq":
                rows = [r for r in rows if getattr(r, c[1]) == c[2]]
            elif isinstance(c, tuple) and c[0] == "is":
                rows = [r for r in rows if getattr(r, c[1]) is c[2]]
        return Q(rows, self.cols)
    def order_by(self, *a): return self
    def all(self): return [tuple(getattr(r, c.name) for c in self.cols) if self.cols else r for r in self.rows]
    def first(self): a = self.all(); return a[0] if a else None
    def __iter__(self): return iter(self.all())
    def update(self, values, synchronize_session=False):
        n = 0
        for r in self.rows:
            for k, v in values.items():
                setattr(r, k.name if isinstance(k, Col) else k, v); n += 1
        return n
class DB:
    def __init__(self, users, accounts, owned=None):
        self.users, self.accounts, self.owned = users, accounts, owned or {}
        self.commits = 0
    def query(self, *what):
        target = what[0]
        model = target if isinstance(target, type) else None
        cols = None
        if model is None:
            # column queries: (Model.col, ...)
            cols = list(what)
            name = cols[0].name
            model = AdAccount if name in ("advertiser_id", "owner_bc_id") or any(c.name == "advertiser_id" for c in cols) else User
            if name == "owner_user_id" and cols[0] is AdAccount.owner_user_id:
                model = AdAccount
        rows = {User: self.users, AdAccount: self.accounts}.get(model, self.owned.get(model, []))
        return Q(rows, cols)
    def get(self, model, pk): return next((u for u in self.users if u.id == pk), None)
    def commit(self): self.commits += 1

janis, marta, bob = User(1, "janis@glitchy.ai"), User(2, "marta@x.com"), User(3, "bob@x.com", active=False)
accounts = [AdAccount("100", 1, "bc1"), AdAccount("200", 2, "bc2"), AdAccount("201", 2, "bc2"), AdAccount("300", None, "bc3")]
db = DB([janis, marta, bob], accounts)
class Req:
    def __init__(self, cookie=None, user=None):
        self.cookies = {scope.COOKIE: cookie} if cookie else {}
        self.state = types.SimpleNamespace(user=user)

print("\n-- who sees what --")
sc = scope.current(Req("u:1", marta), db, marta)
check("a buyer gets their own workspace whatever the cookie says", sc.mode == "user" and sc.user_id == 2 and sc.ids == {"200", "201"} and not sc.can_switch)
check("…and can't see another buyer's account", not sc.allows("100") and sc.allows("201") and not sc.allows(""))
check("…nor an unowned one", not sc.allows("300"))
sc = scope.current(Req(None, janis), db, janis)
check("super admin, no cookie → Everyone (exactly what the dashboard showed before)", sc.mode == "all" and sc.ids is None and sc.everything and sc.can_switch and sc.allows("300") and sc.allows("200"))
sc = scope.current(Req("u:2", janis), db, janis)
check("super admin looking at Marta sees Marta's accounts, labelled by her email", sc.mode == "user" and sc.ids == {"200", "201"} and sc.label == "marta@x.com" and sc.can_switch)
check("…and anything created there belongs to Marta", sc.owner_for_new == 2)
sc = scope.current(Req("u:1", janis), db, janis)
check("super admin on their own workspace: label Mine", sc.label == "Mine" and sc.user_id == 1 and sc.ids == {"100"})
check("an inactive user's view falls back to Everyone", scope.current(Req("u:3", janis), db, janis).mode == "all")
check("an unknown user id falls back to Everyone", scope.current(Req("u:99", janis), db, janis).mode == "all" and scope.current(Req("garbage", janis), db, janis).mode == "all")
check("not logged in → nothing", scope.current(Req(), db, None).ids == set())
check("Everyone creates for the super admin themselves", scope.current(Req(None, janis), db, janis).owner_for_new == 1)

print("\n-- owned rows --")
sc_m = scope.current(Req(), db, marta)
check("owns(): Marta's preset yes, Janis's no", sc_m.owns(Owned(2)) and not sc_m.owns(Owned(1)) and not sc_m.owns(Owned(None)))
check("owns() on Everyone: everything", scope.current(Req(None, janis), db, janis).owns(Owned(2)))
q = sc_m.owned(Q([Owned(1), Owned(2), Owned(2)]), models.Template)
check("owned() filters a query to the view", len(q.all()) == 2 and all(r.owner_user_id == 2 for r in q.all()))
check("owned() is a no-op on Everyone", len(scope.current(Req(None, janis), db, janis).owned(Q([Owned(1), Owned(2)]), models.Template).all()) == 2)
check("filter_ids keeps only the view's", sc_m.filter_ids(["100", "200", "300", "201"]) == ["200", "201"])

print("\n-- switch, claim, backfill --")
opts = scope.switch_options(db, scope.current(Req("u:2", janis), db, janis))
check("switch: Everyone, Mine, then other active users (inactive left out)", [o["label"] for o in opts] == ["Everyone", "Mine", "marta@x.com"] and opts[2]["on"] and opts[1]["value"] == "u:1")
check("a buyer has no switch", scope.switch_options(db, sc_m) == [])
pooled = AdAccount("300", None)
check("claim(): an unowned account goes to the launcher", scope.claim(db, pooled, 2) and pooled.owner_user_id == 2)
owned_acc = AdAccount("100", 1)
check("claim(): an owned account never moves", not scope.claim(db, owned_acc, 2) and owned_acc.owner_user_id == 1)
check("claim(): unknown user / no user → nothing", not scope.claim(db, AdAccount("301", None), 99) and not scope.claim(db, AdAccount("302", None), None))
rows = {models.Template: [Owned(None), Owned(2)], models.Creative: [Owned(None)]}
db2 = DB([janis, marta], [AdAccount("300", None)], rows)
n = scope.backfill(db2)
check("backfill(): owner-less rows and accounts go to the super admin; owned ones untouched",
      n == 3 and rows[models.Template][0].owner_user_id == 1 and rows[models.Template][1].owner_user_id == 2 and db2.accounts[0].owner_user_id == 1 and db2.commits == 1, str(n))
check("backfill() with no super admin user is a no-op", scope.backfill(DB([marta], [AdAccount("9", None)])) == 0)

print("\n-- an Admin grant (v155.21): a member may open selected users' dashboards --")
eva = User(4, "eva@x.com")
db3 = DB([janis, marta, bob, eva], accounts + [AdAccount("400", 4, "bc4")])
marta.can_view = {4, 3}                      # eva (active) and bob (deactivated)
sc = scope.current(Req("u:4", marta), db3, marta)
check("Marta, granted Eva: the cookie opens Eva's workspace, labelled by her email", sc.mode == "user" and sc.user_id == 4 and sc.ids == {"400"} and sc.label == "eva@x.com" and sc.can_switch)
check("…and anything created there belongs to Eva", sc.owner_for_new == 4)
check("…but never the owner's, an ungranted user's or a deactivated one's (back to Mine)",
      all(scope.current(Req(c, marta), db3, marta).user_id == 2 for c in ("u:1", "u:3", "u:99")) and scope.current(Req("u:1", marta), db3, marta).label == "Mine")
check("…and never Everyone", scope.current(Req(None, marta), db3, marta).ids == {"200", "201"} and not scope.current(Req(None, marta), db3, marta).everything)
opts = scope.switch_options(db3, scope.current(Req("u:4", marta), db3, marta))
check("her switch: Mine, then the granted users only (no Everyone, no owner, no deactivated)", [o["label"] for o in opts] == ["Mine", "eva@x.com"] and opts[1]["on"], str(opts))
check("may_view(): the grant, herself; not the owner, not the ungranted", scope.may_view(db3, marta, 4) and scope.may_view(db3, marta, 2) and not scope.may_view(db3, marta, 1) and not scope.may_view(db3, marta, 3))
check("the owner may view anyone", scope.may_view(db3, janis, 2) and not scope.may_view(db3, janis, 99))
marta.can_view = set()
check("no grant: a plain member again", not scope.current(Req("u:4", marta), db3, marta).can_switch and scope.current(Req("u:4", marta), db3, marta).user_id == 2)
def read(rel): return open(os.path.join(ROOT, rel), encoding="utf-8").read()
tm, mn, sp, st = read("app/routes/team.py"), read("app/main.py"), read("app/routes/settings_page.py"), read("app/templates/settings.html")
check("POST /view honours the grant and never gives a grantee Everyone", "scope_mod.may_view(db, me, uid)" in tm and 'value = "all" if owner else f"u:{me.id}"' in tm and 'elif not owner:\n        value = f"u:{me.id}"' in tm)
check("what a grantee creates in that view belongs to the viewed user", "picked in _users.viewable_ids(user)" in mn and "owner = view = picked" in mn)
check("the top-bar switch shows for grantees", "users.is_owner(me) or users.viewable_ids(me)" in read("app/templating.py"))
check("Settings › Users: owner-only route stores the grant (active, non-owner users only); the card has the pop-up",
      '@router.post("/settings/users/{user_id}/views")' in sp and "users.set_viewable(db, u, ids)" in sp and "not is_owner(u)" in read("app/users.py")
      and 'data-act="views"' in st and "admin · views" in st and 'f("views").ids.value = ids.join(",")' in st)
check("Team page and the Users card stay the owner's", "if not users.is_owner(me):" in tm and "{% if view_switch.owner %}<div class=\"vs-foot\">" in read("app/templates/base.html"))
check("a grantee's Settings page shows and saves the viewed user's settings, labelled as theirs — never the server cards, Users, 2FA admin or the Access log",
      "other = bool(me is not None and uid is not None and uid != me.id)" in sp and '"own_view": bool(me is not None and users.is_owner(me) and not other)' in sp
      and "return me if users.is_owner(me) else None" in sp and st.count("{% if sec.own_view %}") >= 4)
check("moving accounts between workspaces stays the owner's (Everyone view)", 'view.can_switch and view.everything %}<span class="ac-owner-bulk"' in read("app/templates/accounts.html"))
check("parse_cookie", scope.parse_cookie("u:7") == 7 and scope.parse_cookie("all") is None and scope.parse_cookie("u:x") is None and scope.parse_cookie(None) is None)

# ==== static checks over the shipped files ==============================================
print("\n-- static: models, guards, pages --")
def read(rel): return open(os.path.join(ROOT, rel), encoding="utf-8").read()
import re
msrc = read("app/models.py")
for name in scope.OWNED_MODELS + ("BusinessCenter",):
    body = re.search(rf"^class {name}\(Base\):.*?(?=^class |\Z)", msrc, re.M | re.S).group(0)
    check(f"models.{name} has owner_user_id", "owner_user_id = Column(Integer" in body)
    if name != "BusinessCenter":
        check(f"models.{name}.owner_user_id defaults to the request's owner", "default=ctx.owner_default" in body)
check("BusinessCenter carries the login's token", "access_token = Column(EncryptedText" in re.search(r"^class BusinessCenter\(Base\):.*?(?=^class )", msrc, re.M | re.S).group(0))
dbsrc = read("app/database.py")
for t in ("ad_accounts", "templates", "creatives", "display_cards", "ad_texts", "spark_codes", "spark_code_groups", "tags", "business_centers"):
    check(f"schema_additions adds owner_user_id to {t}", re.search(rf'"{t}": \{{[^}}]*"owner_user_id": "INTEGER"', dbsrc, re.S) is not None)
check("launch_queue gets launched_by", '"launched_by": "INTEGER"' in dbsrc)
ctx = read("app/ctx.py"); mainsrc = read("app/main.py")
check("ctx has OWNER + VIEW context vars, set by the auth middleware", "OWNER: ContextVar" in ctx and "VIEW: ContextVar" in ctx and "_ctx.OWNER.set(owner)" in mainsrc and "_ctx.VIEW.set(view)" in mainsrc)
check("backfill runs at start", "_scope.backfill(_d)" in mainsrc)
check("team router registered", "team.router" in mainsrc)

# every account-path route is guarded
n_routes = n_guard = 0
for fn in os.listdir(os.path.join(ROOT, "app", "routes")):
    if not fn.endswith(".py"): continue
    src = read(f"app/routes/{fn}")
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if re.match(r'@router\.(get|post)\("[^"]*\{advertiser_id\}', line):
            n_routes += 1
            sig = "\n".join(lines[i:i + 8])
            if "guard.account_in_view" in sig: n_guard += 1
            else: print("   unguarded:", fn, line)
        if re.match(r'@router\.(get|post)\("[^"]*\{creative_id\}', line):
            n_routes += 1
            sig = "\n".join(lines[i:i + 8])
            if "guard.creative_in_view" in sig: n_guard += 1
            else: print("   unguarded:", fn, line)
check(f"every account / creative route is guarded ({n_guard} of {n_routes})", n_routes >= 25 and n_guard == n_routes)
g = read("app/routes/guard.py")
check("the guard answers 404 (no hint) outside the view", 'raise HTTPException(status_code=404, detail="Not in your view")' in g)

pages = {
    "app/routes/dashboard.py": ["pnl_data.overall_totals(db, start_utc, end_utc, ids)", "inbox_mod.build(db, sc)", "creative_perf.rows(db, start_utc, end_utc, today=True, owner_user_id=sc.user_id)", "if sc.allows(a.advertiser_id)]", '"/accounts/owner"'],
    "app/routes/status.py": ["if not sc.allows(r.advertiser_id):          # another user's account — shares above still count it", "tags_mod.all_tags(db, sc.user_id)", "owner_user_id=sc.owner_for_new"],
    "app/routes/pnl_page.py": ["slices = _slices(db, start_utc, end_utc, sc)", "pnl_data.overall_totals(db, start_utc, end_utc, ids)", "scope_mod.view_sources(db, sc)", '"shared": shared'],
    "app/routes/audience.py": ["ids = sc.filter_ids(ids) if ids is not None else sorted(sc.ids or [])"],
    "app/routes/inbox.py": ["inbox_mod.build(db, scope_mod.for_request(request, db))"],
    "app/routes/alerts.py": ["key = (sc.mode, sc.user_id)", "_BELL_CACHE[key]"],
    "app/routes/monitor.py": ["inbox_mod.build(db, sc)", "if sc.allows(a.advertiser_id)]"],
    "app/routes/performance.py": ["pnl_data.overall_totals(db, start_utc, end_utc, sc.ids)", "scope_mod.event_in_view(e, sc, srcs)"],
    "app/routes/team.py": ['@router.post("/view")', '@router.get("/team")', '@router.get("/team/{user_id}/detail.json")', "pnl_data.overall_totals(db, start_utc, end_utc, ids)", "timeutil.range_bounds(range_key, start, end)", "resp.set_cookie(scope_mod.COOKIE"],
    "app/routes/super_launcher.py": ["sc.owned(db.query(models.Template), models.Template)", 'fields["_launched_by"] = sc.owner_for_new', "owner_user_id=sc.user_id,", "if sc.allows(a) and a not in skip]"],
    "app/routes/campaigns.py": ["_scope.claim(db, acct, fields.get(\"_launched_by\"))", "_owned(db.query(models.Creative), models.Creative, fields)", "_owned(db.query(models.AdText), models.AdText, fields)", 'fields["_launched_by"] = sc.owner_for_new'],
    "app/queue_worker.py": ["launched_by=launched_by", 'fields["_launched_by"] = item.launched_by or template.owner_user_id', "owner_user_id=(item.launched_by or template.owner_user_id)"],
    "app/routes/templates_routes.py": ["sc.owned(db.query(models.Template), models.Template)", "models.Template(owner_user_id=sc.owner_for_new)", "if not t or not sc.owns(t):", "scope_mod.pixels_in_view(db, sc,"],
    "app/routes/spark_codes.py": ["sc.owned(db.query(models.SparkCode), models.SparkCode)", "owner_user_id=sc.owner_for_new", "def _group_in_view"],
    "app/routes/ad_texts.py": ["sc.owned(db.query(models.AdText), models.AdText)", "owner_user_id=sc.owner_for_new"],
    "app/routes/display_cards.py": ["owner_user_id=sc.owner_for_new", "if not card or not sc.owns(card):"],
    "app/routes/creatives.py": ["sc.owned(db.query(models.Creative), models.Creative)", "_owned_creatives(db).filter_by(md5=md5)", "def _owned_creatives"],
    "app/routes/pixels.py": ["scope_mod.pixels_in_view(db, sc,", "_scope(db, scope, sc)"],
    "app/routes/oauth.py": ["user_id=_owner_for(request, db))", "auth_mod.current_user(request, db)", "mine_before = {", "if row.advertiser_id in mine_before and row.advertiser_id not in seen_ids", "bc_row.access_token = access_token", "queries.distinct_tokens(db)"],
    "app/balances.py": ["queries.token_for_bc(db, bc.bc_id)", "queries.distinct_tokens(db)"],
    "app/queries.py": ["def token_for_bc", "def token_for_user", "def distinct_tokens"],
    "app/inbox.py": ["def build(db: Session, scope=None)", "if lost[0] and ids is None:"],
    "app/assistant.py": ["scope_mod.from_ctx(db)", "_slices(db, s, e, _scope(db))", "inbox_mod.build(db, sc)"],
    "app/routes/appeals_page.py": ["if not row or not sc.allows(row.advertiser_id):", "if sc.allows(r.advertiser_id)]"],
    "app/routes/automation.py": ["i.launched_by == sc.user_id or sc.allows(i.advertiser_id)"],
    "app/tags.py": ["is taken by another user", "def get_in_view", "owner_user_id: int | None = None) -> dict[str, list[dict]]"],
    "app/pnl_data.py": ["def source_weights", "def overall_totals(db: Session, start_utc: datetime, end_utc: datetime, advertiser_ids=None)", '**({"shared": True} if w < 1.0 else {})'],
    "app/hourly.py": ["weights: dict[str, float] | None = None"],
    "app/templates/base.html": ['action="/view"', "view_switch.options"],
    "app/templates/team.html": ['value="u:{{ r.user.id }}"', '<div data-daterange></div>', '/team/"+uid+"/detail.json?', 'daterange.js'],
    "app/templates/accounts.html": ['data-ac="owner"', 'class="pill dim ac-owner"'],
    "app/static/accounts.js": ['UI.post("/accounts/owner"'],
    "app/templates/pnl.html": [">shared</span>"],
    "app/templating.py": ["def view_switch(request", 'ctx.setdefault("view_switch"'],
    "app/nav.py": ['("/team", "Team"'],
}
for rel, needles in pages.items():
    src = read(rel)
    for nd in needles:
        check(f"{rel}: {nd[:60]}", nd in src)

# no leftover global picks in the launch engine's "next unused" queries
eng = read("app/routes/campaigns.py")
seg = eng[eng.index("def launch_to_account"):eng.index("def run_batch(")]
check("launch engine: every 'next unused' creative / text pick is owner-scoped",
      'db.query(models.Creative).filter_by(status="available"' not in seg and 'db.query(models.AdText).filter_by(status="available"' not in seg
      and "db.query(models.AdText)\n                             .filter_by(status=\"available\")" not in seg)
sl = read("app/routes/super_launcher.py")
check("super launcher never launches to an account outside the view", 'advertiser_ids = [a for a in form.getlist("advertiser_ids") if sc.allows(a) and a not in skip]' in sl)
check("STATIC_VERSION bumped", "STATIC_VERSION = \"176\"" in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
