"""v153 — TikTok test mode (a local simulator behind tiktok_api's one HTTP client) and
numbered database migrations. The simulator is driven through the REAL tiktok_api functions."""
import contextlib, importlib, os, sqlite3, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

print("-- refused on a server --")
src = read("app/config.py")
check("test mode needs ADOPS_MOCK_TIKTOK=1 AND a non-server host",
      'MOCK_TIKTOK = MOCK_TIKTOK_ASKED and not ON_SERVER' in src and "was IGNORED" in read("app/main.py"))

os.environ.pop("RENDER", None); os.environ.pop("ADOPS_DEPLOYED", None)
os.environ["ADOPS_MOCK_TIKTOK"] = "1"
os.environ["MOCK_REVIEW_S"] = "0"
for m in [k for k in sys.modules if k == "app.config" or k == "app.tiktok_api" or k == "app.tiktok_mock"]:
    del sys.modules[m]
sys.modules["app.diag"] = types.SimpleNamespace(record=lambda *a, **k: None)
cfg = importlib.import_module("app.config")
check("on a local machine it's on", cfg.MOCK_TIKTOK is True)
api = importlib.import_module("app.tiktok_api")
mock = importlib.import_module("app.tiktok_mock")
api.time = types.SimpleNamespace(sleep=lambda s: None, time=__import__("time").time)

print("-- the simulator, through the real client --")
bcs = api.list_business_centers("tok")
check("business centers", bcs and bcs[0]["bc_info"]["bc_id"] == mock.BC_ID)
check("the BC's ad accounts", [a["asset_id"] for a in api.list_bc_advertisers("tok", mock.BC_ID)["list"]] == [a[0] for a in mock.ACCOUNTS])
info = api.get_advertiser_info("tok", ["7100000000000000001"])
check("advertiser info with its own timezone", info[0]["timezone"] == "Etc/GMT+5" and info[0]["status"] == "STATUS_ENABLE")
check("BC identity and its posts", api.list_identities("tok", "7100000000000000001", identity_type="BC_AUTH_TT", identity_authorized_bc_id=mock.BC_ID)[0]["identity_type"] == "BC_AUTH_TT"
      and len(api.list_tt_videos("tok", "7100000000000000001", "mock-bc-identity-1", "BC_AUTH_TT", identity_authorized_bc_id=mock.BC_ID)["list"]) == len(mock.POSTS))
adv = "7100000000000000001"
try:
    api.create_campaign("tok", adv, {"campaign_name": "Offer failx2", "objective_type": "WEB_CONVERSIONS"})
    first = "created"
except api.TikTokError as e:
    first = str(e.code)
try:
    api.create_campaign("tok", adv, {"campaign_name": "Offer failx2", "objective_type": "WEB_CONVERSIONS"})
    second = "created"
except api.TikTokError as e:
    second = str(e.code)
cid = str(api.create_campaign("tok", adv, {"campaign_name": "Offer failx2", "objective_type": "WEB_CONVERSIONS"}).get("campaign_id"))
check("failxN: the first N creates fail, then it works", first == "40002" and second == "40002" and cid.startswith("18"))
ag = api.create_adgroup("tok", adv, {"campaign_id": cid, "adgroup_name": "g"})["adgroup_id"]
ad = api.create_ad("tok", adv, {"adgroup_id": ag, "creatives": [{"ad_name": "a"}]})
check("ad group and ad created", ag.startswith("17") and ad["ad_ids"])
rej = str(api.create_campaign("tok", adv, {"campaign_name": "Test REJECT me"})["campaign_id"])
rag = api.create_adgroup("tok", adv, {"campaign_id": rej})["adgroup_id"]
api.create_ad("tok", adv, {"adgroup_id": rag, "creatives": [{"ad_name": "r"}]})
ads = {a["campaign_id"]: a for a in api.list_ads("tok", adv)["list"]}
check("REJECT: its ads come back disapproved; the other delivers (review time 0 here)",
      ads[rej]["secondary_status"] == "AD_STATUS_AUDIT_DENY" and ads[cid]["secondary_status"] == "AD_STATUS_DELIVERY_OK")
rows = api.get_report("tok", adv, dimensions=["campaign_id"], metrics=["spend"], start_date="2026-09-01", end_date="2026-09-01")
check("delivering campaigns report numbers (deterministic); rejected ones none",
      [r["dimensions"]["campaign_id"] for r in rows] == [cid] and float(rows[0]["metrics"]["spend"]) > 0
      and api.get_report("tok", adv, dimensions=["campaign_id"], metrics=["spend"], start_date="x", end_date="x")[0]["metrics"] == rows[0]["metrics"])
try:
    api.authorize_tt_video("tok", adv, "CODE-bad-123")
    bad = "accepted"
except api.TikTokError as e:
    bad = e.message
sk = importlib.import_module("app.spark_check")
check("'bad' in a spark code: refused — and spark_check calls it the code's fault", "invalid" in bad and sk.classify(40002, bad) == "bad")
api.update_campaign_status("tok", adv, [cid], "DISABLE")
check("status updates stick", [c for c in api.list_campaigns("tok", adv)["list"] if c["campaign_id"] == cid][0]["operation_status"] == "DISABLE")
mock.set_outage(True)
try:
    api.list_business_centers("tok")
    out = "answered"
except api.TikTokError as e:
    out = str(e.code)
mock.set_outage(False)
check("outage: every call fails (and ends when switched off)", out == "50000" and api.list_business_centers("tok"))
check("an unknown endpoint answers empty, never crashes", api.api_get("/some/new/endpoint/", "tok", {}) ["list"] == [])
check("Diagnostics has the switch (owner only, 404 when test mode is off)",
      '@router.post("/diagnostics/mock/{action}")' in read("app/routes/diagnostics.py") and "if not config.MOCK_TIKTOK or not guard.is_owner(request):" in read("app/routes/diagnostics.py")
      and 'id="testmode"' in read("app/templates/diagnostics.html") and "{% if MOCK_TIKTOK %}" in read("app/templates/base.html"))

print("-- numbered migrations --")
mg = importlib.import_module("app.migrations")
class Conn:
    def __init__(self, c): self.c = c
    def exec_driver_sql(self, sql, params=()):
        return self.c.execute(sql, params)
class Engine:
    def __init__(self): self.db = sqlite3.connect(":memory:")
    @contextlib.contextmanager
    def begin(self):
        try:
            yield Conn(self.db)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
eng = Engine()
d = eng.db
d.executescript("""
CREATE TABLE ad_accounts (advertiser_id TEXT, geo_policy TEXT, status_changed_at TEXT);
INSERT INTO ad_accounts VALUES ('A', NULL, '2026-09-01 10:00:00+00:00'), ('B', 'us_only', '2026-09-02 10:00:00');
CREATE TABLE campaign_records (campaign_id TEXT, advertiser_id TEXT);
INSERT INTO campaign_records VALUES ('1811', 'A');
CREATE TABLE alerts (kind TEXT, ref_id TEXT);
INSERT INTO alerts VALUES ('rule_action', '1811'), ('rule_action', 'A'), ('account_error', '1811');
CREATE TABLE profile_posts (via TEXT);
INSERT INTO profile_posts VALUES (NULL), ('code');
""")
r = mg.run(eng)
check("every step applied in order, recorded with rows touched", [a["version"] for a in r["applied"]] == [1, 2, 3, 4, 5] and r["failed"] is None)
check("…the data fixes did their job",
      d.execute("SELECT geo_policy FROM ad_accounts ORDER BY advertiser_id").fetchall() == [("any",), ("us_only",)]
      and d.execute("SELECT ref_id FROM alerts ORDER BY rowid").fetchall() == [("A",), ("A",), ("1811",)]
      and d.execute("SELECT via FROM profile_posts ORDER BY rowid").fetchall() == [("bc",), ("code",)]
      and d.execute("SELECT status_changed_at FROM ad_accounts WHERE advertiser_id='A'").fetchone()[0] == "2026-09-01 10:00:00")
check("a second boot applies nothing", mg.run(eng)["applied"] == [] and len(mg.applied(eng)) == 5)
boom = lambda conn: (_ for _ in ()).throw(RuntimeError("nope"))
import logging; logging.getLogger("adops.migrations").disabled = True      # the failure is expected here
r = mg.run(eng, steps=mg.STEPS + [(6, "breaks", boom), (7, "after", lambda c: 0)])
check("a failing step stops the run (logged, retried next boot), nothing after it runs",
      r["failed"].startswith("6 breaks") and r["applied"] == [] and len(mg.applied(eng)) == 5)
try:
    mg.pending(set(), [(2, "a", None), (1, "b", None)])
    order_ok = False
except AssertionError:
    order_ok = True
check("numbers must be unique and ascending", order_ok and [s[0] for s in mg.pending({1, 2})] == [3, 4, 5])
check("run at boot right after the schema, shown on Diagnostics",
      "_migrations.run(_engine)" in read("app/main.py") and read("app/main.py").index("_migrations.run(_engine)") > read("app/main.py").index("init_db()\n")
      and 'id="migrations"' in read("app/templates/diagnostics.html"))

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
