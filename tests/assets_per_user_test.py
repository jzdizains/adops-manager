"""The Assets (/bc-assets) page is unique per user (v144).

Before this, the whole assets subsystem was global: one main-BC choice, one watchlist,
one chosen-pixel set, one audit snapshot and one wire report — shared by everyone — and
the scan read EVERY user's ad accounts. So an invited buyer opened Assets and saw the
owner's 569 accounts and Business Centers, and changing the main BC changed it for all.

Now every setting key is namespaced by the viewing user (`key::u<id>`) and every account
/ pixel / token read is restricted to that user's workspace. `user_id=None` keeps the
ORIGINAL global key and every account — the super-admin's "Everyone" view, the launch
path and the old tests are all unchanged. The scan/wire/connect jobs carry the user id in
their payload so a background job reads and writes only its own user's data.

bc_assets can't be imported here (it pulls in sqlalchemy + the app package), so the pure
helpers are extracted and executed, the per-user key model is simulated against the REAL
`_skey`, and the threading is asserted against source."""
import os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

BA = read("app/bc_assets.py")
def grab(src, name):
    i = src.index("def " + name + "(")
    ends = [e for e in (src.find(m, i + 1) for m in ("\ndef ", "\n@", "\nclass ")) if e != -1]
    return src[i:(min(ends) if ends else len(src))]

# ---- 1. the real _skey, executed --------------------------------------------------
ns = {}
exec(grab(BA, "_skey"), ns)
_skey = ns["_skey"]
print("-- setting keys are namespaced per user, None stays global --")
check("a user id namespaces the key", _skey("main_bc_id", 7) == "main_bc_id::u7")
check("None keeps the ORIGINAL global key (Everyone view / single-user / launch path)",
      _skey("main_bc_id", None) == "main_bc_id")
check("different users get different keys", _skey("bc_assets_snapshot", 1) != _skey("bc_assets_snapshot", 2))
check("a string-ish id is coerced, not concatenated raw", _skey("k", 12) == "k::u12")

# ---- 2. the isolation model, simulated against the real _skey ---------------------
print("-- two users, one global view: nobody sees anyone else's assets --")
store: dict[str, str] = {}
def setk(base, uid, val): store[_skey(base, uid)] = val
def getk(base, uid): return store.get(_skey(base, uid), "")
setk("main_bc_id", 1, "BC_ONE")
setk("main_bc_id", 2, "BC_TWO")
setk("main_bc_id", None, "BC_GLOBAL")          # the Everyone view / pre-upgrade value
check("user 1 reads their own main BC", getk("main_bc_id", 1) == "BC_ONE")
check("user 2 reads a DIFFERENT main BC (not user 1's)", getk("main_bc_id", 2) == "BC_TWO")
check("the Everyone/global view is separate again", getk("main_bc_id", None) == "BC_GLOBAL")
check("a brand-new user starts empty — no leak from the shared past", getk("main_bc_id", 3) == "")
check("one user changing theirs never touches another's",
      (setk("main_bc_id", 1, "CHANGED") or getk("main_bc_id", 2)) == "BC_TWO")

# ---- 3. the job payload carries the user; older/global jobs read as None -----------
print("-- background jobs are stamped with the workspace they belong to --")
jh = read("app/job_handlers.py")
nsj = {}
exec(grab(jh, "_job_user_id"), nsj)
_job_user_id = nsj["_job_user_id"]
check("a job payload with a user id yields that id", _job_user_id({"user_id": 5}) == 5)
check("a payload without one (older job / Everyone view) is None → global", _job_user_id({}) is None)
check("a junk value degrades to None, never crashes the handler", _job_user_id({"user_id": "x"}) is None)

# ---- 4. bc_assets threads user_id through every public entry point -----------------
print("-- bc_assets: every read/write takes a user_id and scopes to it --")
for fn in ("tokens", "main_bc_id", "chosen_pixels", "set_chosen_pixels", "watchlist",
           "watch_add", "watch_remove", "snapshot", "snapshot_at", "_store", "scan",
           "last_wire", "_store_wire", "_admin_tokens", "stage", "wire", "connect_bc",
           "members", "invite"):
    check(f"{fn}() accepts a user_id", "user_id" in grab(BA, fn).split("\n")[0] or f"user_id" in grab(BA, fn)[:200])
check("account reads are restricted to the user's workspace",
      "models.AdAccount.owner_user_id == int(user_id)" in BA)
check("the account-row list is scoped by owner (and still lists token-less accounts)",
      "acct_rows = db.query(models.AdAccount)" in BA and "acct_rows.filter(models.AdAccount.owner_user_id == int(user_id))" in BA)
check("the pixel guess for a user only counts that user's pixels",
      "_my_advertisers(db, user_id)" in BA and "owner_advertiser_id" in BA)
check("the snapshot is stored under the per-user key",
      "_skey(SNAPSHOT_KEY, user_id)" in BA and "_skey(SNAPSHOT_AT_KEY, user_id)" in BA)
check("main BC / watchlist / pixels / wire all use the per-user key",
      all(f"_skey({k}, user_id)" in BA for k in ("MAIN_BC_KEY", "WATCH_KEY", "PIXELS_KEY", "WIRE_KEY")))
check("no bare global setting read/write is left in bc_assets",
      "get_setting(db, SNAPSHOT_KEY" not in BA and "get_setting(db, MAIN_BC_KEY" not in BA
      and "get_setting(db, WATCH_KEY" not in BA and "upsert_setting(db, WIRE_KEY," not in BA)

# ---- 5. the route feeds the viewing user through everything ------------------------
print("-- the /bc-assets route uses the scope's user for every call --")
rt = read("app/routes/bc_assets_page.py")
check("it derives the user from the request scope", "scope_mod.for_request(request, db).user_id" in rt)
check("the page reads the per-user snapshot / main BC / watchlist / wire",
      "bc_assets.snapshot(db, uid)" in rt and "bc_assets.main_bc_id(db, uid)" in rt
      and "bc_assets.watchlist(db, uid)" in rt and "bc_assets.last_wire(db, uid)" in rt)
check("scan/wire/connect stamp the user into the job payload",
      'body["user_id"] = uid' in rt and "_enqueue_user(db," in rt)
check("running/wiring/connecting flags are per-user, not global-by-kind",
      "_pending_for(db, (\"bc_assets_scan\",), uid)" in rt and "jobs.pending(db," not in rt)
check("the state poll is scoped to the viewing user",
      "def bc_assets_state(request:" in rt and "_pending_for(db, _JOB_KINDS, uid)" in rt)
check("setting the main BC writes the per-user key",
      "_skey(bc_assets.MAIN_BC_KEY, uid)" in rt)
check("connect can run for two users at once (dedup is per-user, not one-at-a-time-by-kind)",
      "_pending_for(db, (kind,), uid)" in rt)

# ---- 6. the job handlers pass the payload's user through --------------------------
print("-- the scan/wire/connect handlers scope to the job's user --")
check("scan passes the job's user id", "user_id=uid" in jh and "_job_user_id(p)" in jh)
check("wire and connect pass the job's user id", jh.count("user_id=_job_user_id(p)") >= 2)

# ---- 7. the launch path and Partners are deliberately UNCHANGED (still global) -----
print("-- launch + Partners keep the global main BC (no behaviour change there) --")
camp = read("app/routes/campaigns.py")
check("launch still reads the global main BC (user_id defaults to None)",
      "return bc_assets.main_bc_id(db)" in camp)
pp = read("app/routes/partners_page.py")
check("the owner-only Partners page still calls members/invite globally",
      "bc_assets.members(db, bid)" in pp and "bc_assets.invite(db, bid, email, role)" in pp)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
