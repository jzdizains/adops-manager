"""v152 — profile-post picker: BC-linked and spark-code identities merged into one profile,
day groups, launched / new badges, the big player with ← → and launch history."""
import importlib, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

# profile_videos imports sqlalchemy + models at the top: stand-ins so the pure merge runs here
for name in ("sqlalchemy", "sqlalchemy.orm"):
    if name not in sys.modules:
        m = types.ModuleType(name); m.Session = object; sys.modules[name] = m
sys.modules.setdefault("app.models", types.ModuleType("app.models"))
sys.modules.setdefault("app.tiktok_api", types.ModuleType("app.tiktok_api"))
pv = importlib.import_module("app.profile_videos")

V = lambda i: {"item_id": i, "text": "t" + i}
bc = [{"identity_id": "BC1", "identity_type": "BC_AUTH_TT", "name": "Lucy", "videos": [V("1"), V("2")]}]
code = [{"identity_id": "C9", "identity_type": "AUTH_CODE", "name": "lucy ", "videos": [V("2"), V("3")]},
        {"identity_id": "C7", "identity_type": "AUTH_CODE", "name": "mark", "videos": [V("8")]}]
out = pv.merge_code(bc, code)
lucy = out[0]
check("a code identity's posts join the BC profile with the same handle", [v["item_id"] for v in lucy["videos"]] == ["1", "2", "3"])
check("a post on both stays the Business-Center one (code-free launch path)", lucy["videos"][1]["via"] == "bc" and "identity_type" not in lucy["videos"][1])
check("a code-only post carries its code identity", lucy["videos"][2]["via"] == "code" and lucy["videos"][2]["identity_id"] == "C9" and lucy["videos"][2]["identity_type"] == "AUTH_CODE")
check("a code identity without a BC twin becomes its own profile", len(out) == 2 + 0 and out[1]["name"] == "mark" and out[1]["code_only"] and out[1]["videos"][0]["via"] == "code")
check("inputs aren't mutated", [v["item_id"] for v in bc[0]["videos"]] == ["1", "2"])

src = read("app/profile_videos.py")
check("the listing reads the account's spark-code identities too (bounded) and merges them",
      'identity_type="AUTH_CODE")[:CODE_MAX]' in src and "profiles = merge_code(profiles, code_profiles)" in src and "CODE_MAX = 30" in src)
check("…kept in the database copy (via / via_identity)", "via=v.get(\"via\") or \"\"" in src and 'via = Column(String, default="")' in read("app/models.py"))
check("a code post launches with its code, never as a BC profile post",
      'if str(it.get("identity_type") or "") == "AUTH_CODE":' in src and "row.code = str(it[\"auth_code\"])" in src)
sl = read("app/routes/super_launcher.py")
check("launch history of a post: this workspace's launches only, with verdict and spend",
      '@router.get("/super-launcher/post-history.json")' in sl and "sc.owned(db.query(models.SparkCode.id), models.SparkCode)" in sl
      and "logs = [lg for lg in logs if sc.allows(lg.advertiser_id)]" in sl)
pj = read("app/static/picker.js")
check("picker: day groups (Today / Yesterday / date), never-launched first inside a day",
      'return "Today"' in pj and 'return "Yesterday"' in pj and 'class="pv-day"' in pj and "(launches(a) > 0) - (launches(b) > 0)" in pj)
check("picker: launched N× / new badge, code marker", 'class="pv-lch' in pj and 'class="pv-via"' in pj)
check("picker: ← → move the preview; the big player has ← →, Add/Remove and the launch history",
      'e.key === "ArrowRight"' in pj and "function big(itemId)" in pj and "/super-launcher/post-history.json?item_id=" in pj and "pvb-toggle" in pj)
check("picker: key listeners removed when it closes (no leak)", 'document.removeEventListener("keydown", onKey)' in pj and 'document.removeEventListener("keydown", bk)' in pj)
check("picker: a code post's pick carries AUTH_CODE and its own identity", 'identity_type: code ? "AUTH_CODE" : p.identity_type' in pj)
css = read("app/static/style.css")
check("styles for the new pieces", ".pv-day {" in css and ".pvb-media {" in css and ".pk-thumb .pv-lch" in css)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
