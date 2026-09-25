"""Profile-video picker: favorites (v143).

"Pick profile videos" used to open on ALL profiles across every Business Center (the
operator saw "all random tiktok profiles"). Now each profile can be starred, the choice
is saved per user, and the picker opens on the ★ Favorites view by default — with "All
profiles" still one click away. Star state persists via /super-launcher/profile-favorite.

JS parse + source/structure asserts (the picker is DOM/UI code; the route needs the app
to boot)."""
import os, sys, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

js = read("app/static/picker.js")
print("-- the picker learned favorites --")
check("favorites come in as an option and default the view (v153: as part of Mine)", "o.favorites" in js and 'pf = mine.length ? "__mine__" : ""' in js)
check("a ★ Mine filter shows starred (and recently used) profiles", 'value="__mine__"' in js and 'if (pf === "__mine__") return isMine(p);' in js and "isFav(p) || isUsed(p)" in js)
check("each profile header has a star toggle", 'class="pv-star' in js and 'data-idn="' in js)
check("tapping the star flips it, persists via the callback, and never double-defaults after a manual pick",
      "o.onToggleFav" in js and "userPickedFilter = true" in js and "if (!userPickedFilter) pf =" in js)
check("the empty favorites view tells the operator how to add some", 'None of yours yet' in js and 'tap ☆ on the ones you use' in js)

print("-- js parses --")
r = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync('app/static/picker.js','utf8'))"], cwd=ROOT, capture_output=True, text=True)
check("picker.js is syntactically valid", r.returncode == 0, r.stderr[:200])

print("-- launcher passes favorites + persists them --")
t = read("app/templates/super_launcher.html")
check("the launcher hands the picker the saved favorites and a save callback",
      "FAV_PROFILES = {{ fav_profiles_json|js }}" in t and "favorites: FAV_PROFILES" in t and "onToggleFav: saveFav" in t)
check("saving posts to the per-user endpoint", 'fetch("/super-launcher/profile-favorite"' in t)

print("-- route: per-user favorites, no TikTok call --")
r2 = read("app/routes/super_launcher.py")
check("a POST toggles one profile in this user's list", '@router.post("/super-launcher/profile-favorite")' in r2 and "queries.set_setting(db, _fav_key(sc)" in r2)
check("favorites are keyed per user and seeded into the page", 'f"fav_profiles:{sc.user_id' in r2 and '"fav_profiles_json": json.dumps(_get_favs(db, sc))' in r2 and "**picker_prefs(db, sc)" in r2)
check("add/remove logic is present", "favs.append(idn)" in r2 and "favs = [x for x in favs if x != idn]" in r2)

print("-- css for the star, both themes (token-based) --")
css = read("app/static/style.css")
check("the star has styles and an 'on' state using tokens (no hard-coded theme colors except the gold star)",
      ".pv-star {" in css and ".pv-star.on {" in css and "var(--text-dim)" in css)
check("STATIC_VERSION bumped", 'STATIC_VERSION = "184"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
