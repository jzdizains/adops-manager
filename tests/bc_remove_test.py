"""v155.27 — Remove a Business Center that is no longer used: hidden from the Accounts page, Home
and every picker, its accounts switched off, no balance sweep / low-balance alert for it, the
account sync never switches those accounts back on. Nothing deleted; Restore undoes it."""
import ast, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

src = read("app/routes/dashboard.py")
ns = {"Session": object}
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name == "_retire_bc":
        exec(compile(ast.Module([node], []), "dash", "exec"), ns)
BC = lambda bc_id, retired=False: types.SimpleNamespace(bc_id=bc_id, name="BC " + bc_id, retired=retired)
A = lambda adv, bc, enabled=True, status="": types.SimpleNamespace(advertiser_id=adv, owner_bc_id=bc, enabled=enabled, status=status)
class Col:
    def __init__(self, n): self.n = n
    def __eq__(self, v): return ("eq", self.n, v)
    __hash__ = object.__hash__
class Q:
    def __init__(self, rows): self.rows = rows
    def filter_by(self, **kw): return Q([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])
    def filter(self, *conds): return Q([r for r in self.rows if all(getattr(r, c[1]) == c[2] for c in conds)])
    def first(self): return self.rows[0] if self.rows else None
    def __iter__(self): return iter(self.rows)
class DB:
    def __init__(self, bcs, accts): self.bcs, self.accts, self.commits = bcs, accts, 0
    def query(self, m): return Q(self.bcs if m is M.BusinessCenter else self.accts)
    def commit(self): self.commits += 1
M = types.SimpleNamespace(BusinessCenter=type("BusinessCenter", (), {}), AdAccount=type("AdAccount", (), {"owner_bc_id": Col("owner_bc_id")}))
ns["models"] = M
bcs = [BC("1"), BC("2")]
accts = [A("100", "1"), A("101", "1", enabled=False, status="ACCESS_LOST"), A("200", "2")]
everything = types.SimpleNamespace(everything=True, allows=lambda a: True)
mine = types.SimpleNamespace(everything=False, allows=lambda a: a in ("100", "101"))

b, n, err = ns["_retire_bc"](DB(bcs, accts), everything, "1", True)
check("remove: the BC is marked retired and its enabled accounts switched off — one write", b.retired and n == 1 and not accts[0].enabled and not err)
check("…a lost-access account stays as it was", accts[1].enabled is False and accts[1].status == "ACCESS_LOST")
check("…the other BC's accounts are untouched", accts[2].enabled)
b, n, err = ns["_retire_bc"](DB(bcs, accts), everything, "1", False)
check("restore: the flag comes off and the switched-off accounts come back (not the lost-access one)", not b.retired and n == 1 and accts[0].enabled and not accts[1].enabled)
check("a buyer may remove a BC whose accounts are all theirs, not another's", ns["_retire_bc"](DB(bcs, accts), mine, "1", True)[2] == ""
      and "isn't in your view" in ns["_retire_bc"](DB(bcs, accts), mine, "2", True)[2])
check("unknown BC → a message, never a crash", ns["_retire_bc"](DB(bcs, accts), everything, "9", True)[2] == "No such Business Center.")

print("-- wiring --")
check("the column exists (added on first start)", "retired = Column(Boolean, default=False)" in read("app/models.py"))
check("routes: remove + restore, audited", '@router.post("/accounts/bc/{bc_id}/remove")' in src and '@router.post("/accounts/bc/{bc_id}/restore")' in src
      and '"bc.removed"' in src and '"bc.restored"' in src)
check("Accounts page hides removed BCs and their accounts unless showing hidden; Home skips them",
      "retired_ids = {b.bc_id for b in all_bcs if b.retired}" in src and 'a.status != "ACCESS_LOST" and (a.owner_bc_id or "") not in retired_ids' in src
      and "bcs = all_bcs if show_lost else [b for b in all_bcs if not b.retired]" in src and "if not b.retired]" in src.split("def overview(")[1][:6000])
tpl = read("app/templates/accounts.html")
check("the group header: Remove (asks first) / Restore, a 'removed' pill, the hidden link counts removed BCs",
      'action="/accounts/bc/{{ g.key }}/remove"' in tpl and 'action="/accounts/bc/{{ g.key }}/restore"' in tpl and ">removed</span>" in tpl
      and "removed BC" in tpl and "ac-bc-remove" in read("app/static/accounts.js") and "UI.confirm" in read("app/static/accounts.js").split("ac-bc-remove")[1][:600])
bal = read("app/balances.py")
check("no balance sweep, no BC account listing, no low-balance alert for a removed BC", bal.count("or bc.retired:") == 2 and "if bc.retired:\n            continue" in bal)
oa = read("app/routes/oauth.py")
check("the account sync never switches a removed BC's accounts back on", "retired_bcs = {r[0] for r in db.query(models.BusinessCenter.bc_id).filter(models.BusinessCenter.retired == True)}" in oa
      and 'not in retired_bcs:\n            row.enabled = True' in oa)
check("pickers and launchers only ever see enabled accounts, so switched-off ones are gone there too", "queries.enabled_accounts(db)" in read("app/routes/super_launcher.py"))
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
