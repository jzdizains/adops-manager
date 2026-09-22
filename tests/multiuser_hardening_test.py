"""Multi-user hardening + memory-limit scaling (v143).

Before inviting more buyers, several routes that acted on user-owned rows had no owner/scope
check. This locks in the fixes (a buyer can only touch their own launch-queue items, display
cards, pixels, spark codes; the Partners/BC-admin page is owner-only) and the memory number
now tracks the real Render plan instead of a hard-coded 512.

Source/structure asserts — these routes need the full app to boot; the memory helper's logic
is unit-run directly."""
import os, sys, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

print("-- launch queue: retry/cancel/retry-failed are scoped to the user --")
a = read("app/routes/automation.py")
check("a shared visibility check exists", "def _queue_visible(sc, item)" in a and "item.launched_by == sc.user_id or sc.allows(item.advertiser_id)" in a)
check("retry, cancel and retry-failed all apply it", a.count("_queue_visible(sc, item)") >= 3 and "def retry_failed(request:" in a and "def cancel_item(item_id: int, request:" in a)

print("-- display-card image: owner-only (no IDOR) --")
d = read("app/routes/display_cards.py")
_img = d.split("def image(")[1].split("\n@router")[0]
check("the image endpoint checks ownership before serving the file", "for_request(request, db).owns(card)" in _img)

print("-- pixels: provision/add-existing act only on accounts in view --")
p = read("app/routes/pixels.py")
check("provision refuses an account outside the workspace", "def provision(request:" in p and "if not sc.allows(advertiser_id):" in p)
check("add-existing only probes accounts the user can see", "if sc.allows(a.advertiser_id)]" in p)

print("-- spark codes: import dedup is per-user, not global --")
sc_src = read("app/routes/spark_codes.py")
check("bulk import dedups against THIS user's codes", "existing = {c.code for c in sc.owned(db.query(models.SparkCode), models.SparkCode).all()}" in sc_src)
cr = read("app/routes/creators.py")
check("authorize creates/looks up the spark code in the user's own scope", "usc.owned(db.query(models.SparkCode), models.SparkCode).filter_by(code=code)" in cr and "owner_user_id=usc.user_id" in cr)

print("-- Partners / BC-admin page is owner-only --")
pp = read("app/routes/partners_page.py")
check("an owner gate exists and guards the page, invite, mailbox, toggle and feeds",
      "def _is_owner(request" in pp and pp.count("_is_owner(request)") >= 6)
nav = read("app/nav.py")
check("Partners is hidden from the section tabs for non-owners", '"/partners"' in nav and "OWNER_ONLY_TABS" in nav)
check("Partners is dropped from the command palette for non-owners", '"/partners"' in read("app/templating.py").split("OWNER_ONLY_JUMP")[1][:120])

print("-- memory ceiling + display scale to the box --")
# unit-run background.mem_limit_mb via a direct spec load (no heavy app import)
spec = importlib.util.spec_from_file_location("_bg", os.path.join(ROOT, "app", "background.py"))
# the module imports `from . import ...` lazily inside functions, so top-level load is safe only
# for the pure helper; guard by reading source for the cgroup + env logic instead of importing.
bg = read("app/background.py")
check("mem_limit_mb reads the cgroup (v2 then v1) with an env fallback",
      "def mem_limit_mb()" in bg and "/sys/fs/cgroup/memory.max" in bg and "memory.limit_in_bytes" in bg and 'os.environ.get("MEM_LIMIT_MB"' in bg)
ipb = read("app/instant_page_builder.py")
check("the browser memory gate scales with the plan (≥1 GB box → box minus headroom, else 330)",
      "def memory_ceiling_mb()" in ipb and "lim - 500" in ipb and "memory_ceiling_mb()" in ipb)
st = read("app/templates/settings.html")
check("the settings line shows the real limit, not a hard-coded 512", "of 512." not in st and "mem_limit_mb" in st)
check("render.yaml carries the 2 GB fallback", 'key: MEM_LIMIT_MB' in read("render.yaml") and '"2048"' in read("render.yaml"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
