"""Assets audit (/bc-assets) — read-only inventory of pixel + profile links.

The audit must: read every stored token's Business Centers, use the main BC's admin
token, list its pixels/profiles/ad accounts, read what each is linked to, and report
per ad account what is missing — without ever writing to TikTok, and without falling
over when one call fails.
"""
import os, sys, tempfile
os.environ.setdefault("ADOPS_DISABLE_BG", "1")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="adops-bca-")
from unittest import mock
from fastapi.testclient import TestClient
from app.main import app
from app import bc_assets, config, models, queries, tiktok_api
from app.database import SessionLocal

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

MAIN, SAT = "111", "222"
db = SessionLocal()
db.add(models.AdAccount(advertiser_id="A1", advertiser_name="blue bat 1", access_token="tok-main", enabled=True, status="ENABLE", owner_bc_id=MAIN))
db.add(models.AdAccount(advertiser_id="A2", advertiser_name="blue bat 2", access_token="tok-sat", enabled=True, status="ENABLE", owner_bc_id=SAT))
db.add(models.AdAccount(advertiser_id="A3", advertiser_name="blue bat 3", access_token="tok-sat", enabled=True, status="ENABLE", owner_bc_id=SAT))
db.add(models.PixelRecord(pixel_id="PX1", pixel_name="Main pixel", owner_bc_id=MAIN))
db.commit()

BCS = {
    "tok-main": [{"bc_info": {"bc_id": MAIN, "name": "Main BC", "status": "ENABLE"}, "user_role": "ADMIN"}],
    "tok-sat": [{"bc_info": {"bc_id": SAT, "name": "Satellite BC", "status": "ENABLE"}, "user_role": "ADMIN"},
                 {"bc_info": {"bc_id": MAIN, "name": "Main BC", "status": "ENABLE"}, "user_role": "STANDARD"}],
}
ASSETS = {
    ("111", "PIXEL"): [{"asset_id": "PX1", "asset_name": "Main pixel"}],
    ("111", "TT_ACCOUNT"): [{"asset_id": "TT1", "asset_name": "Creator one", "tt_asset_handle": "@one"},
                            {"asset_id": "TT2", "asset_name": "Creator two", "tt_asset_handle": "@two"}],
    ("111", "ADVERTISER"): [{"asset_id": "A1"}, {"asset_id": "A2"}],      # A3 not shared in yet
}
calls = {"writes": 0}
def fake_bcs(token):
    return BCS.get(token, [])
def fake_assets(token, bc_id, asset_type, max_pages=20):
    return ASSETS.get((bc_id, asset_type), [])
def fake_pixel_links(token, bc_id, pixel_id, page=1, page_size=50):
    return {"list": [{"advertiser_id": "A1"}]}
def fake_tt_links(token, bc_id, asset_id, asset_type="TT_ACCOUNT", max_pages=20):
    return {"TT1": ["A1", "A2"], "TT2": ["A1"]}.get(asset_id, [])
def no_writes(*a, **k):
    calls["writes"] += 1
    raise AssertionError("the audit must not write to TikTok")

P = [mock.patch.object(tiktok_api, "list_business_centers", side_effect=fake_bcs),
     mock.patch.object(tiktok_api, "bc_assets_admin", side_effect=fake_assets),
     mock.patch.object(tiktok_api, "bc_pixel_link_get", side_effect=fake_pixel_links),
     mock.patch.object(tiktok_api, "bc_tt_account_advertisers", side_effect=fake_tt_links),
     mock.patch.object(tiktok_api, "bc_pixel_link_update", side_effect=no_writes),
     mock.patch.object(tiktok_api, "bc_tt_account_link", side_effect=no_writes),
     mock.patch.object(tiktok_api, "bc_partner_add", side_effect=no_writes)]
for p in P: p.start()
try:
    snap = bc_assets.scan(db)
finally:
    for p in P: p.stop()

check("the main BC is found from the pixel the dashboard knows", snap["main_bc"] == MAIN and snap["main_bc_name"] == "Main BC", str(snap.get("main_bc")))
check("every BC the stored tokens can see is listed, with the role that matters", {b["bc_id"]: b["role"] for b in snap["bcs"]} == {MAIN: "ADMIN", SAT: "ADMIN"} and snap["admin_on_main"], str(snap["bcs"]))
check("the main BC's pixels and profiles are listed", [p["id"] for p in snap["pixels"]] == ["PX1"] and [p["handle"] for p in snap["profiles"]] == ["@one", "@two"])
rows = {r["advertiser_id"]: r for r in snap["accounts"]}
check("an ad account shared into the main BC with pixel + every profile reads as fully wired",
      rows["A1"]["in_main_bc"] and rows["A1"]["pixels"] == ["Main pixel"] and rows["A1"]["profiles_missing"] == 0, str(rows["A1"]))
check("an account in the BC but missing the pixel and one profile is reported as such",
      rows["A2"]["in_main_bc"] and rows["A2"]["pixels"] == [] and rows["A2"]["profiles_missing"] == 1, str(rows["A2"]))
check("an account that was never shared into the main BC is flagged", rows["A3"]["in_main_bc"] is False and rows["A3"]["profiles"] == [], str(rows["A3"]))
check("the summary counts what is left to do", snap["summary"] == {"accounts": 3, "in_main_bc": 2, "with_pixel": 1, "all_profiles": 1, "ready": 1, "profiles": 2, "pixels": 1}, str(snap["summary"]))
check("nothing was written to TikTok", calls["writes"] == 0)

# a failing read becomes a note, never an exception
def boom(*a, **k):
    raise tiktok_api.TikTokError("40001", "no permission")
with mock.patch.object(tiktok_api, "list_business_centers", side_effect=fake_bcs), \
     mock.patch.object(tiktok_api, "bc_assets_admin", side_effect=fake_assets), \
     mock.patch.object(tiktok_api, "bc_pixel_link_get", side_effect=boom), \
     mock.patch.object(tiktok_api, "bc_tt_account_advertisers", side_effect=fake_tt_links):
    snap2 = bc_assets.scan(db)
check("a read that TikTok refuses is recorded on the asset, the audit still finishes", snap2["pixels"][0].get("error") and snap2["summary"]["accounts"] == 3, str(snap2["pixels"][0]))

# the page renders the stored snapshot without calling TikTok at all
c = TestClient(app); c.post("/login", data={"email": config.OWNER_EMAIL, "password": config.APP_PASSWORD})
with mock.patch.object(tiktok_api, "list_business_centers", side_effect=no_writes):
    h = c.get("/bc-assets").text
check("the page renders from the snapshot, no live calls", "blue bat 3" in h and "Main BC" in h and calls["writes"] == 0)
check("the gaps filter hides the account that needs nothing", "blue bat 1" not in c.get("/bc-assets?show=gaps").text)
check("…and All shows everything", "blue bat 1" in c.get("/bc-assets?show=all").text)
r = c.post("/bc-assets/main", data={"bc_id": " 999x "}, follow_redirects=False)
check("the main BC id is cleaned to digits before it is stored", queries.get_setting(SessionLocal(), bc_assets.MAIN_BC_KEY, "") == "999" and r.status_code == 303)
r = c.post("/bc-assets/scan", follow_redirects=False)
check("the audit runs as a background job, not during the request", r.status_code == 303 and SessionLocal().query(models.Job).filter_by(kind="bc_assets_scan").count() == 1)
db.close()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
