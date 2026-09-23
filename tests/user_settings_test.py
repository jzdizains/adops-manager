"""Per-user settings (v119).

Functional (settings_store with a stubbed Setting table):
- GLOBAL_KEYS / USER_KEYS split every default exactly once
- the pre-workspaces row migrates into the owner's row, SAME postback key
- another user starts from the defaults with a fresh key of their own
- a buyer's save never touches the global row; the owner's save does
- an empty postback key on save keeps the stored one
- user_for_postback_key: each user's key → them; a wrong key → nobody; the legacy
  global key → the owner
- per_user(): every active user with their own accounts (unowned → the owner)
Static (the shipped files):
- the background sweep runs rules / top-ups / inventory per user
- rules filter by the user's accounts and P&L share
- the postback endpoint resolves the key to a user; launches use the launcher's settings;
  renames / source check / appeals use the account owner's; pages use the view
- Settings page: Users, Access log, Server, Assistant and font only on the owner's own
  view; the save route drops GLOBAL_KEYS for anyone else; a member's recent logins are
  their own; the ⌘K list hides owner-only pages outside the own view
Runs without any dependency."""
import json, os, sys, types

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

class Col:
    def __init__(self, name): self.name = name
    def __eq__(self, o): return ("eq", self.name, o)
    def like(self, p): return ("like", self.name, p)
    __hash__ = object.__hash__
class Setting:
    key = Col("key"); value = Col("value")
    def __init__(self, key, value): self.key, self.value = key, value
class User:
    id = Col("id"); email = Col("email"); active = Col("active")
    def __init__(self, id, email, active=True): self.id, self.email, self.active = id, email, active
class AdAccount:
    advertiser_id = Col("advertiser_id"); owner_user_id = Col("owner_user_id")
    def __init__(self, adv, owner=None): self.advertiser_id, self.owner_user_id = adv, owner
models = _mod("app.models", Setting=Setting, User=User, AdAccount=AdAccount)
OWNER = User(1, "janis@glitchy.ai"); BUYER = User(2, "ab@glitchy.com"); GONE = User(3, "old@x.com", active=False)
_mod("app.scope", super_admin=lambda db: OWNER)

class Q:
    def __init__(self, rows, cols=None): self.rows = list(rows); self.cols = cols
    def filter(self, *conds):
        rows = self.rows
        for c in conds:
            if c[0] == "eq": rows = [r for r in rows if getattr(r, c[1]) == c[2]]
            elif c[0] == "like": rows = [r for r in rows if str(getattr(r, c[1])).startswith(c[2].rstrip("%"))]
        return Q(rows, self.cols)
    def filter_by(self, **kw): return Q([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())], self.cols)
    def order_by(self, *a): return self
    def all(self): return [tuple(getattr(r, c.name) for c in self.cols) if self.cols else r for r in self.rows]
    def first(self): a = self.all(); return a[0] if a else None
    def scalar(self): a = self.all(); return (a[0][0] if self.cols else a[0]) if a else None
    def __iter__(self): return iter(self.all())
class DB:
    def __init__(self):
        self.settings = []; self.users = [OWNER, BUYER, GONE]; self.accounts = []
        self.commits = 0
    def query(self, *what):
        if what[0] is Setting: return Q(self.settings)
        if what[0] is User: return Q(self.users)
        if what[0] is AdAccount: return Q(self.accounts)
        return Q(self.accounts, what)      # column tuples (advertiser_id, owner_user_id)
    def commit(self): self.commits += 1
def _row(db, key):
    return next((r for r in db.settings if r.key == key), None)
def insert_if_absent(db, key, value):
    if _row(db, key) is None: db.settings.append(Setting(key, value))
def upsert(db, key, value):
    r = _row(db, key)
    if r is None: db.settings.append(Setting(key, value))
    else: r.value = value
_mod("app.queries", insert_setting_if_absent=insert_if_absent, upsert_setting=upsert)
_mod("app.ctx", OWNER=types.SimpleNamespace(get=lambda: None))

import importlib
ss = importlib.import_module("app.settings_store")

print("\n-- key split --")
check("every default is exactly one of global / user", set(ss.GLOBAL_KEYS) | set(ss.USER_KEYS) == set(ss.DEFAULTS) and not (set(ss.GLOBAL_KEYS) & set(ss.USER_KEYS)))
check("tracking, postback, rules, top-ups, events, appeals are per user",
      {"postback_key", "tracking_mode", "url_param", "rules_enabled", "topup_enabled", "events_api_enabled", "appeal_auto_enabled", "clickflare_field", "min_fresh_accounts"} <= set(ss.USER_KEYS))
check("sweeps, queue pacing, audience refresh, assistant model are server-wide",
      {"sweep_interval_sec", "queue_per_sweep", "launch_pace_sec", "audience_hours_every_min", "assistant_model"} <= set(ss.GLOBAL_KEYS))

print("\n-- migration --")
db = DB()
legacy = {**ss.DEFAULTS, "postback_key": "6d0" * 10 + "ab", "tracking_mode": "clickflare", "url_param": "src", "sweep_interval_sec": 45, "rules_enabled": True}
db.settings.append(Setting(ss.KEY, json.dumps(legacy)))
o = ss.get_settings(db)                       # no user → the owner
check("the owner inherits the pre-workspaces row (same key, same tracking mode)", o["postback_key"] == legacy["postback_key"] and o["tracking_mode"] == "clickflare" and o["url_param"] == "src" and o["rules_enabled"] is True)
check("…and the server-wide keys come from the global row", o["sweep_interval_sec"] == 45)
check("the owner's row now exists on its own", _row(db, ss.user_key(1)) is not None and json.loads(_row(db, ss.user_key(1)).value)["postback_key"] == legacy["postback_key"])
b = ss.get_settings(db, BUYER.id)
check("a buyer starts from the defaults", b["tracking_mode"] == "direct" and b["url_param"] == "source" and b["rules_enabled"] is False)
check("…with their own fresh postback key", len(b["postback_key"]) == 32 and b["postback_key"] != o["postback_key"])
check("…and the same server-wide values", b["sweep_interval_sec"] == 45)
check("a second read is stable (no re-mint)", ss.get_settings(db, BUYER.id)["postback_key"] == b["postback_key"])

print("\n-- saving --")
ss.save_settings(db, {**b, "tracking_mode": "redirect", "sweep_interval_sec": 99, "postback_key": ""}, user_id=BUYER.id, global_too=False)
b2 = ss.get_settings(db, BUYER.id)
check("a buyer's save lands in their row", b2["tracking_mode"] == "redirect")
check("…never in the global row", b2["sweep_interval_sec"] == 45 and ss.get_settings(db)["sweep_interval_sec"] == 45)
check("an empty postback key keeps the stored one", b2["postback_key"] == b["postback_key"])
check("…and the owner's row is untouched", ss.get_settings(db)["tracking_mode"] == "clickflare")
ss.save_settings(db, {**o, "sweep_interval_sec": 90, "rule_cpa_max": 12.0}, user_id=OWNER.id, global_too=True)
check("the owner's save writes both layers", ss.get_settings(db)["sweep_interval_sec"] == 90 and ss.get_settings(db)["rule_cpa_max"] == 12.0 and ss.get_settings(db, BUYER.id)["sweep_interval_sec"] == 90)
check("…without touching the buyer's rules", ss.get_settings(db, BUYER.id)["rule_cpa_max"] == 0.0)

print("\n-- postback key → user --")
check("the buyer's key is the buyer", ss.user_for_postback_key(db, b["postback_key"]) == BUYER.id)
check("the owner's key is the owner", ss.user_for_postback_key(db, o["postback_key"]) == OWNER.id)
check("a wrong key is nobody", ss.user_for_postback_key(db, "nope") is None and ss.user_for_postback_key(db, "") is None)
db2 = DB(); db2.settings.append(Setting(ss.KEY, json.dumps({"postback_key": "legacykey123"})))
check("the legacy global key (Glitchy already has it) still works → the owner", ss.user_for_postback_key(db2, "legacykey123") == OWNER.id)

print("\n-- per_user --")
db.accounts = [AdAccount("111", 1), AdAccount("222", 2), AdAccount("333", None)]
rows = ss.per_user(db)
check("one entry per ACTIVE user, in id order", [u.id for u, _, _ in rows] == [1, 2])
check("each with their own accounts; an unowned account counts as the owner's", rows[0][2] == {"111", "333"} and rows[1][2] == {"222"})
check("each with their own settings", rows[1][1]["tracking_mode"] == "redirect" and rows[0][1]["tracking_mode"] == "clickflare")
check("for_account → the owner's settings for an account", ss.for_account(db, "222")["tracking_mode"] == "redirect" and ss.for_account(db, "333")["tracking_mode"] == "clickflare")

print("\n-- static: consumers --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
bg = read("app/background.py")
check("the sweep runs rules per user over their accounts", "for u, us, ids in settings_store.per_user(db):" in bg and "rules.evaluate_pause_rules(db, us, ids)" in bg and "rules.evaluate_profit_rules(db, us, ids)" in bg)
check("…top-ups and inventory too", "rules.evaluate_topups(db, us, ids)" in bg and "rules.check_fresh_inventory(db, us, u.id)" in bg and "rules.check_pool_inventory(db, us, u.id)" in bg)
ru = read("app/rules.py")
check("pause rules skip other users' campaigns and use the user's P&L share", ru.count("if ids is not None and rec.advertiser_id not in ids:") >= 2 and ru.count("pnl_data.source_pnl(db, start, end, ids)") == 2)
check("top-ups skip other users' accounts", "if ids is not None and acct.advertiser_id not in ids:" in ru)
check("fresh-inventory alert is per user", 'eligible_accounts(db, "new_only", 10_000, owner_user_id=user_id)' in ru and 'f"fresh:u{user_id}"' in ru)
check("pool alert is per user's presets and pools", "tq.filter(models.Template.owner_user_id == user_id)" in ru and "q.filter(model.owner_user_id == user_id)" in ru)
check("inbox shows a per-user inventory alert only in that user's view", 'a.ref_id.rsplit(":u", 1)[1] != str(uid)' in read("app/inbox.py"))
pb = read("app/routes/postback.py")
check("the postback key picks the user, and that user's settings apply", "settings_store.user_for_postback_key(db, q.get(\"key\", \"\"))" in pb and "s = get_settings(db, None if uid == -1 else uid)" in pb)
ca = read("app/routes/campaigns.py")
check("launches use the launcher's tracking setup", ca.count('get_settings(db, fields.get("_launched_by"))') >= 4)
check("a rename follows the account owner's source mode", 'settings_store.for_account(db, advertiser_id).get("source_mode"' in ca)
check("Source check page uses the view", "settings_store.for_view(db)" in ca)
sc = read("app/source_check.py")
check("the source audit names each account's own ?source= param", "def param_for(adv: str)" in sc and "verdict(url, param_for(aid), name)" in sc and "settings_store.for_account(db, advertiser_id)" in sc)
ap = read("app/appeals.py")
check("auto-appeals judge each account by its owner's settings and cap", "settings_store._cached(db, uid, scache)" in ap and "filed_today(db, accts_of.get(uid, set()))" in ap)
check("manual appeals use the account owner's reason; the page uses the view", "settings_store.for_account(db, row.advertiser_id, scache)" in read("app/routes/appeals_page.py") and "settings_store.for_view(db)" in read("app/routes/appeals_page.py"))
check("Health page uses the view", "settings_store.for_view(db)" in read("app/routes/monitor.py"))
check("queue launches judge account health by the owner's thresholds", "settings_store.for_account(db, acct.advertiser_id)" in read("app/queue_worker.py"))
check("boot migrates the legacy row", "_ss.get_settings(_d)" in read("app/main.py"))

print("\n-- static: Settings page --")
sp = read("app/routes/settings_page.py")
check("the page shows the viewed workspace's settings", 'ws = _workspace(request, db)' in sp and 's = get_settings(db, ws["user_id"])' in sp)
check("save: server-wide keys only from the owner's own view", 'if key in settings_store.GLOBAL_KEYS and not ws["own_view"]:' in sp and 'save_settings(db, values, user_id=ws["user_id"], global_too=ws["own_view"])' in sp)
check("Users / Access log only on the owner's own view", '"access": sec.access_log(db, 80) if own_view else []' in sp and 'db.query(models.User).order_by(models.User.email).all() if own_view else []' in sp)
check("a member's recent logins are their own", 'email=(me.email if me else "")' in sp and "q.filter(models.LoginAttempt.email == email)" in read("app/auth_security.py"))
check("caption font upload is owner-only", 'if not users.is_owner(getattr(request.state, "user", None)):' in sp)
check("test events use the viewed workspace's pixel setup", 'get_settings(db, _workspace(request, db)["user_id"])' in sp)
th = read("app/templates/settings.html")
check("nav: Users, Access log, Server, Assistant, font gated by own_view; no is_owner gate left on them", th.count("{% if sec.own_view %}") >= 5 and '{% if sec.is_owner %}<a href="#users"' not in th and 'data-tab="server"' in th)
check("server-wide inputs live in the owner-only Server section", th.index('{% if sec.own_view %}\n<section class="stab" data-tab="server">') < th.index('name="queue_per_sweep"') < th.index('name="sweep_interval_sec"') < th.index('name="assistant_model"') < th.index("{% endif %}\n<div class=\"save-bar\""))
check("editing another user shows a banner", "{% if ws.other %}" in th and "Editing <b>{{ ws.email }}</b>'s settings" in th)
check("⌘K hides owner-only pages outside the own view", 'OWNER_ONLY_JUMP = frozenset({"/settings#users", "/settings#access", "/team"})' in read("app/templating.py"))
check("STATIC_VERSION bumped", 'STATIC_VERSION = "157"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
