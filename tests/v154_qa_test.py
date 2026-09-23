"""v154 — fixes from clicking through the live v153: Blocked dated by when the issue scan first
saw the problem; the profile picker opens on "Mine" (starred + recently launched) with Hide;
local post dates, whole-second durations, own cover copies, arrow keys safe."""
import importlib, os, sys, types
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

bn = importlib.import_module("app.board_numbers")
acct = types.SimpleNamespace(advertiser_id="A", status_changed_at=None)
iss = {("c", "C1"): datetime(2026, 9, 7, 10, 0), ("a", "A"): datetime(2026, 9, 5, 9, 0)}
check("Blocked: the campaign's own issue sighting wins", bn.error_at({"since": None}, acct, True, "C1", iss) == datetime(2026, 9, 7, 10, 0))
check("…then the account's (a punished account)", bn.error_at(None, acct, True, "C2", iss) == datetime(2026, 9, 5, 9, 0))
check("…unknown still counts as recent", bn.error_at(None, types.SimpleNamespace(advertiser_id="Z", status_changed_at=None), True, "C9", iss) is None)
st = read("app/routes/status.py")
check("the board reads the issue dates once and treats 'advertiser account punish' as account-level",
      "issue_at = board_numbers.issue_dates(db, models)" in st and '"punish" in b or "advertiser" in b' in st and st.count("r.campaign_id, issue_at)") == 2)

sl = read("app/routes/super_launcher.py")
check("used profiles: identities / creator handles this workspace launched from in 60 days",
      "def used_profiles(db: Session, sc, days: int = USED_DAYS)" in sl and "USED_DAYS = 60" in sl and "sc.owned(db.query(models.SparkCode), models.SparkCode)" in sl.split("def used_profiles")[1][:1500])
check("hide per user, own endpoint", '@router.post("/super-launcher/profile-hide")' in sl and 'f"hidden_profiles:{sc.user_id' in sl)
check("both pickers get favorites, hidden and used", "**picker_prefs(db, sc)" in sl and "**picker_prefs(db, sc)" in read("app/routes/warmup_page.py")
      and "hidden: HIDDEN_PROFILES, used: USED_PROFILES" in read("app/templates/super_launcher.html") and "hidden: HIDDEN, used: USED" in read("app/templates/warmup.html"))
check("own small cover copies replace TikTok's full-size expiring URLs", '"/thumbs/pp/%s.jpg" % v["item_id"]' in sl.split("def profile_videos_json")[1][:1500])
pj = read("app/static/picker.js")
check("picker: opens on ★ Mine (starred + used), All excludes hidden, a Hidden view to undo",
      'pf = mine.length ? "__mine__" : ""' in pj and 'if (pf === "__hidden__") return isHidden(p);' in pj and 'if (isHidden(p)) return false;' in pj and 'value="__hidden__"' in pj)
check("picker: Hide / Unhide on each profile, saved per user; starring a hidden one unhides it",
      'class="btn sm ghost pv-hide"' in pj and 'savePref("/super-launcher/profile-hide", hidn, hon)' in pj and 'if (on && hid[idn])' in pj)
check("picker: tile date is the LOCAL day (matches its day group); durations in whole seconds",
      '(v.created ? " · " + esc(dayKey(v)) : "")' in pj and 'v.duration + "s' not in pj and "Math.round(v.duration)" in pj)
check("picker: arrow keys never throw on a non-element target", "e.target && e.target.closest" in pj and "var tg = e.target && e.target.closest ? e.target : null;" in pj)

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
