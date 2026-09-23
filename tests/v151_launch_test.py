"""v151 — launch: start time in the ad account's own timezone (+60 s), account geo policy,
and the Review step's per-account check (page / form / card / identity / geo, blocked reason)."""
import importlib, os, sys, types
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

# =======================================================================================
print("-- start time in the account's timezone --")
at = importlib.import_module("app.acct_time")
now = datetime(2026, 9, 8, 10, 0, 0, tzinfo=timezone.utc)
check("UTC−5 account (Etc/GMT+5): its clock + 60 s", at.start_now("Etc/GMT+5", now=now) == "2026-09-08 05:01:00", at.start_now("Etc/GMT+5", now=now))
check("UTC+8 account: its clock + 60 s (not 8 h in the past)", at.start_now("Asia/Shanghai", now=now) == "2026-09-08 18:01:00")
check("DST honoured (New York in September = UTC−4)", at.start_now("America/New_York", now=now) == "2026-09-08 06:01:00")
check("offset spellings: UTC+08:00, GMT-5, +0530", at.start_now("UTC+08:00", now=now) == "2026-09-08 18:01:00"
      and at.start_now("GMT-5", now=now) == "2026-09-08 05:01:00" and at.start_now("+0530", now=now) == "2026-09-08 15:31:00")
check("unknown / empty / garbage → UTC (the old behaviour), never an error",
      at.start_now("", now=now) == "2026-09-08 10:01:00" and at.start_now("Mars/Olympus", now=now) == "2026-09-08 10:01:00"
      and at.start_now(None, now=now) == "2026-09-08 10:01:00")
check("naive datetimes are UTC", at.tiktok_time(datetime(2026, 1, 1, 12, 0, 0), "Etc/GMT-3") == "2026-01-01 15:00:00")
check("labels", at.label("Etc/GMT+5") == "UTC−5" and at.label("Asia/Kolkata") == "UTC+5:30" and at.label("UTC") == "UTC" and at.label("") == "UTC (timezone unknown)")

class FakeDB:
    def __init__(self): self.commits = 0
    def commit(self): self.commits += 1
    def rollback(self): pass
calls = []
fake_api = types.SimpleNamespace(get_advertiser_info=lambda tok, ids: (calls.append(ids) or [{"advertiser_id": ids[0], "timezone": "Etc/GMT+7"}]))
sys.modules["app.tiktok_api"] = fake_api
import app
app.tiktok_api = fake_api
acct = types.SimpleNamespace(advertiser_id="111", access_token="t", timezone="")
db = FakeDB()
check("an account the sync never filled: asked from TikTok once and stored", at.account_tz(db, acct) == "Etc/GMT+7" and acct.timezone == "Etc/GMT+7" and db.commits == 1)
check("…then read from the row (no second call)", at.account_tz(db, acct) == "Etc/GMT+7" and len(calls) == 1)
fake_api.get_advertiser_info = lambda tok, ids: (calls.append(ids) or [])
acct2 = types.SimpleNamespace(advertiser_id="222", access_token="t", timezone="")
check("TikTok doesn't say → '' and not asked again for an hour", at.account_tz(db, acct2) == "" and at.account_tz(db, acct2) == "" and len(calls) == 2)
del sys.modules["app.tiktok_api"]; del app.tiktok_api

cp = read("app/routes/campaigns.py")
check("launch: the account's timezone is read once per account, before anything is created",
      '"_account_tz": acct_time.account_tz(db, acct)' in cp
      and cp.index('"_account_tz": acct_time.account_tz(db, acct)') < cp.index("# -- config validation FIRST"))
check("every 'start now' goes through it (manual, Smart+, Smart+ Instant Form, the end-time retry)",
      cp.count("acct_time.start_now(") == 4 and 'datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")' not in cp)
check("ad-group copies that need a future start use the account's clock too", "acct_time.start_now(tz_name, lead_s=600)" in read("app/adgroup_copy.py")
      and "_future_start(payload, acct_time.account_tz(db, acct))" in read("app/adgroup_copy.py"))
oa = read("app/routes/oauth.py")
check("the sync fills the timezone of EVERY account (100 per call), not just the first 100",
      "for chunk in (ids[i:i + 100]" in oa and "[:100]" not in oa.split("enrich with advertiser info")[1].split("accounts_synced_at")[0])

# =======================================================================================
print("-- geo policy --")
gf = importlib.import_module("app.geo_fit")
US, FR, DE = "6252001", "3017382", "2921044"
pf = gf.policy_fit
check("any: everything passes", pf("any", [US, FR], [FR])[0] and pf(None, None, [US])[0])
check("us_only: US-only launches only", pf("us_only", None, [US])[0] and not pf("us_only", None, [US, FR])[0] and not pf("us_only", None, [FR])[0])
check("non_us: never a US launch", pf("non_us", None, [FR])[0] and not pf("non_us", None, [US])[0] and not pf("non_us", None, [US, FR])[0])
check("auto: US-unlocked → US launches only", pf("auto", [US, FR], [US])[0] and pf("auto", [US, FR], [FR]) == (False, "policy", "US-unlocked account (Auto) — kept for US launches"))
check("auto: no US → everything but the US", pf("auto", [FR, DE], [FR])[0] and pf("auto", [FR, DE], [US])[1] == "cannot")
check("auto with unknown countries never blocks", pf("auto", None, [US])[0] and pf("auto", None, [FR])[0])
check("no countries wanted (own-country warm-ups) → passes", pf("us_only", None, [])[0])
check("badge", gf.badge([US] + ["x"] * 64) == "US ✓ · 65 countries" and gf.badge([FR]) == "no US · 1 country" and gf.badge(None) == "")
check("the policies offered", gf.POLICY_KEYS == ("any", "auto", "us_only", "non_us"))
md = read("app/models.py")
check("AdAccount.geo_policy, default 'any' (nothing changes until you set it)", 'geo_policy = Column(String, default="any")' in md)
check("launch refuses a policy mismatch before anything is created",
      "geo_fit.policy_fit(acct.geo_policy" in cp and cp.index("geo_fit.policy_fit(acct.geo_policy") < cp.index("spark + pixel resolution BEFORE creating anything"))
sl = read("app/routes/super_launcher.py")
check("auto-pick skips accounts whose policy keeps them for other countries (cache only)",
      "if not geo_ok(a)[0]:" in sl and "geo_fit.cached_targetable(db, a.advertiser_id)" in sl and "fields=fields, exclude=_exclusions(" in read("app/queue_worker.py"))
db_ = read("app/routes/dashboard.py")
check("set per account (drawer) or in bulk (selection bar), scoped to the view",
      '@router.post("/accounts/geo-policy")' in db_ and "sc.allows(x)" in db_.split('"/accounts/geo-policy"')[1][:1200]
      and "/accounts/geo-policy" in read("app/static/accounts.js") and 'data-ac="geo"' in read("app/templates/accounts.html"))

# =======================================================================================
print("-- Review step --")
lr = importlib.import_module("app.launch_review")
C = lr.cell
b, w = lr.verdict({"account": C("ok", "ok"), "page": C("bad", "missing", "no Instant Page “P” on this account"), "geo": C("bad", "US only", "set to US only")})
check("blocked: the FIRST bad column is the reason", b == "no Instant Page “P” on this account" and w == [])
b, w = lr.verdict({"account": C("warn", "cooling down", "cooling"), "page": C("warn", "built at launch", "built from its template"), "identity": C("ok", "x")})
check("warnings don't block", b == "" and w == ["cooling", "built from its template"])
check("a bad identity blocks too", lr.verdict({"identity": C("bad", "none", "no TikTok identity")})[0] == "no TikTok identity")
src = read("app/launch_review.py")
check("no TikTok calls in the table itself (live re-check and identities are opt-in, bounded)",
      "tiktok_api." not in src.split("def review(")[1].split("def sl_main_bc")[0] and "LIVE_MAX = 15" in src and "[:5]" in cp.split("launch_review_identities")[1][:900])
check("same page rules as the launch: page template → built, published sibling + cookies → copied, else blocked",
      '"built at launch"' in src and '"copied at launch"' in src and 'cell(BAD, "missing", f"no Instant Page' in src)
check("identities cached 10 min, bounded", "IDS_TTL = 600" in src and "len(_IDS) > 2000" in src)
check("routes: review.json + identities.json, scoped (preset owned, accounts allowed)",
      '@router.post("/campaigns/review.json")' in cp and '@router.post("/campaigns/review/identities.json")' in cp
      and "sc.owns(template)" in cp.split("def launch_review_json")[1][:800] and "sc.allows(v)" in cp.split("def _review_accounts")[1][:900])
check("blocked accounts are left out: Create Campaign and Super Launcher (manual, auto, queued) honour exclude_ids",
      'v not in skip' in cp.split("async def launch_submit")[1][:800]
      and "exclude=set(_exclude_ids(form))" in sl and "a not in skip]" in sl)
js = read("app/static/launch-review.js")
check("the table: leave-out toggle writes exclude_ids; stale answers dropped; identities 5 at a time",
      'hidden.name = "exclude_ids"' in js and "if (my !== seq) return;" in js and "todo.splice(0, 5)" in js)
for t in ("app/templates/campaign_launch.html", "app/templates/super_launcher.html"):
    h = read(t)
    check(f"{t.split('/')[-1]}: mounted on the Review step, refreshed when it opens",
          "/static/launch-review.js" in h and "LR.mount(" in h and 't === "review" && review' in h)
check("CSS for the table", ".lr-why" in read("app/static/style.css"))

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
