"""Issue recency window (v137): rejected-ad errors older than issue_max_age_days
(default 3) stop showing so the Health/Inbox feed doesn't stack up with weeks-old
rejections. The scan rebuilds issues each run, so an aged-out one just stops
reappearing; appeals are unaffected (only the display is trimmed).

Pure _parse_tt_time / _ad_time (heavy deps stubbed for import) + source/settings asserts."""
import os, sys, types
from datetime import datetime

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

class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()

_mod("sqlalchemy.orm", Session=_Any)
_mod("app"); sys.modules["app"].__path__ = [os.path.join(ROOT, "app")]
_mod("app.models"); _mod("app.queries"); _mod("app.tiktok_api")

import importlib
issues = importlib.import_module("app.issues")

print("-- time parsing --")
check("parses TikTok's 'YYYY-MM-DD HH:MM:SS'", issues._parse_tt_time("2026-09-17 05:51:01") == datetime(2026, 9, 17, 5, 51, 1))
check("empty / garbage → None (kept, never wrongly aged out)", issues._parse_tt_time("") is None and issues._parse_tt_time("nope") is None)
check("_ad_time prefers modify_time over create_time",
      issues._ad_time({"modify_time": "2026-09-20 00:00:00", "create_time": "2026-09-01 00:00:00"}) == datetime(2026, 9, 20, 0, 0, 0))
check("_ad_time falls back to create_time when modify_time is missing",
      issues._ad_time({"create_time": "2026-09-01 00:00:00"}) == datetime(2026, 9, 1, 0, 0, 0))
check("_ad_time with no times → None", issues._ad_time({}) is None)

print("-- scan wiring --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
src = read("app/issues.py")
check("the scan reads the tunable window from settings (default 3)", 'get_settings(db).get("issue_max_age_days", 3)' in src)
check("a cutoff is computed and 0 disables it", "if _max_age > 0 else None" in src and "timedelta(days=_max_age)" in src)
check("old rejected ads are skipped when building issues", "ts = _ad_time(ad)" in src and "if ts is not None and ts < cutoff:" in src)
check("the ad's timestamps are captured for the filter", '"modify_time": ad.get("modify_time", "")' in src)
# appeals must run over the FULL rejected list, before the filtered display loop
check("appeals are unaffected — appeals.sync runs before the recency filter",
      src.index("appeals.sync(db, rejected_ads, scanned)") < src.index("if ts is not None and ts < cutoff:"))

print("-- setting + UI --")
ss = read("app/settings_store.py")
check("issue_max_age_days is a global setting, default 3", '"issue_max_age_days": 3' in ss and '"issue_max_age_days"' in ss.split("GLOBAL_KEYS = frozenset(")[1][:400])
st = read("app/templates/settings.html")
check("Settings exposes the window as an editable number", 'name="issue_max_age_days"' in st and "Drop rejected-ad errors older than" in st)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
