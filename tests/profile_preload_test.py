"""Profile-video picker preload (v139).

"Pick profile videos" used to read Business Centers strictly one at a time on every open,
so it opened at 0 and crawled. Now: a page-session cache (PV_STORE) is warmed in the
background on launcher load (UI.warmProfileVideos, up to 5 BCs at once), and the picker
seeds instantly from that cache and only fetches BCs not already loaded.

Source/structure asserts + a Node syntax check (JS can't be unit-run here)."""
import os, sys, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
js = read("app/static/picker.js")

print("-- shared cache + background warm --")
check("a page-session cache holds posts per Business Center", "var PV_STORE = {}" in js)
check("Business Centers are fetched in parallel (5 at a time), not one-by-one",
      "PV_CONCURRENCY = 5" in js and "function pvPump(" in js and "while (active < PV_CONCURRENCY" in js)
check("UI.warmProfileVideos fills the cache in the background", "UI.warmProfileVideos = function" in js and "pvPump(bcs" in js)
check("a cached BC is served from the store without re-fetching", "if (!refresh && PV_STORE[b.id])" in js)

print("-- the picker opens on what's already warmed --")
check("load() seeds instantly from PV_STORE, then fetches only the pending BCs",
      "instant: show whatever was already warmed" in js
      and "var pending = bcs.filter(function (b) { return refresh || !PV_STORE[b.id]; });" in js
      and "pvPump(pending, refresh," in js)

print("-- launcher warms on page load --")
sl = read("app/templates/super_launcher.html")
check("the launcher preloads profile posts in the background on load",
      "UI.warmProfileVideos && BCS" in sl and "UI.warmProfileVideos(BCS)" in sl and "requestIdleCallback" in sl)

print("-- js parses --")
r = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync('app/static/picker.js','utf8'))"], cwd=ROOT, capture_output=True, text=True)
check("picker.js is syntactically valid", r.returncode == 0, r.stderr[:200])

print("-- version --")
check("STATIC_VERSION bumped", 'STATIC_VERSION = "183"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
