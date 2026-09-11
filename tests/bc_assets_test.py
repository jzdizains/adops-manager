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
# ---- writing: one account, preview then send -------------------------------------
sent = []
def rec_partner(token, bc_id, partner_id, advertiser_ids=None, advertiser_role="OPERATOR"):
    sent.append(("partner_add", token, bc_id, partner_id, list(advertiser_ids or []), advertiser_role)); return {}
def rec_pixel(token, bc_id, pixel_code, advertiser_ids, relation_status="LINK"):
    sent.append(("pixel", token, bc_id, pixel_code, list(advertiser_ids), relation_status)); return {}
def rec_profile(token, bc_id, asset_id, advertiser_id, asset_type="TT_ACCOUNT"):
    sent.append(("profile", token, bc_id, asset_id, advertiser_id)); return {}
W = [mock.patch.object(tiktok_api, "list_business_centers", side_effect=fake_bcs),
     mock.patch.object(tiktok_api, "bc_partner_add", side_effect=rec_partner),
     mock.patch.object(tiktok_api, "bc_pixel_link_update", side_effect=rec_pixel),
     mock.patch.object(tiktok_api, "bc_tt_account_link", side_effect=rec_profile)]
for p_ in W: p_.start()
try:
    prev = bc_assets.wire(db, "A2", role="OPERATOR", dry_run=True)
finally:
    for p_ in W: p_.stop()
eps = [st["request"].get("endpoint") for st in prev["steps"] if st.get("request")]
check("preview sends nothing and lists every request it would make",
      sent == [] and eps == ["/bc/partner/add/", "/bc/pixel/link/update/", "/bc/asset/advertiser/assign/"], str(eps))
check("the share is addressed to the OWNING bc with the main bc as the partner",
      prev["steps"][0]["request"]["bc_id"] == SAT and prev["steps"][0]["request"]["partner_id"] == MAIN
      and prev["steps"][0]["request"]["advertiser_role"] == "OPERATOR", str(prev["steps"][0]["request"]))
check("the pixel request uses pixel_code + relation_status (the v1.3 fields)",
      prev["steps"][1]["request"]["relation_status"] == "LINK" and "pixel_code" in prev["steps"][1]["request"])
for p_ in W: p_.start()
try:
    run = bc_assets.wire(db, "A2", role="OPERATOR", dry_run=False)
finally:
    for p_ in W: p_.stop()
check("send makes exactly those calls, the satellite token sharing and the main token linking",
      [c[0] for c in sent] == ["partner_add", "pixel", "profile"] and sent[0][1] == "tok-sat" and sent[1][1] == "tok-main", str(sent))
check("the run is summarised and stored for the page", "ok" in (run.get("summary") or "") and bc_assets.last_wire(db)["advertiser_id"] == "A2")
sent.clear()
for p_ in W: p_.start()
try:
    again = bc_assets.wire(db, "A1", dry_run=False)      # already has pixel + both profiles
finally:
    for p_ in W: p_.stop()
check("an account that is already wired sends nothing — pressing Wire twice is harmless",
      sent == [] and all(st["ok"] for st in again["steps"]), str([st["detail"] for st in again["steps"]]))
def refuse(*a, **k):
    raise tiktok_api.TikTokError("40001", "no permission for this BC")
with mock.patch.object(tiktok_api, "list_business_centers", side_effect=fake_bcs), \
     mock.patch.object(tiktok_api, "bc_partner_add", side_effect=rec_partner), \
     mock.patch.object(tiktok_api, "bc_pixel_link_update", side_effect=refuse), \
     mock.patch.object(tiktok_api, "bc_tt_account_link", side_effect=rec_profile):
    hurt = bc_assets.wire(db, "A2", dry_run=False)
check("a step TikTok refuses is recorded with its reason and the rest still run",
      any(st["ok"] is False and "no permission" in st["detail"] for st in hurt["steps"]) and "failed" in hurt["summary"], hurt["summary"])
bad = bc_assets.wire(db, "9999", dry_run=True)
check("an ad account the dashboard doesn't manage is refused before anything is attempted",
      (bad.get("error") or "").startswith("9999 is not"), str(bad.get("error")))
r = c.post("/bc-assets/wire", data={"advertiser_id": "A2", "mode": "preview"}, follow_redirects=False)
check("Preview runs as a background job (never during the request)",
      r.status_code == 303 and SessionLocal().query(models.Job).filter_by(kind="bc_assets_wire").count() == 1)

# ---- the journey for a brand-new Business Center ---------------------------------
bc_assets.watch_add(db, SAT2 := "333", "batch 12")
with mock.patch.object(tiktok_api, "list_business_centers", side_effect=fake_bcs):
    st = bc_assets.stage(db, SAT2)
check("a BC no stored token can see reads as invisible — invite and accept still to do",
      st["code"] == "invisible" and "not visible" in st["what"], str(st))
blocked = bc_assets.connect_bc(db, SAT2, dry_run=True)
check("connecting a BC we can't reach is refused with the invite instruction, nothing attempted",
      "Admin of" in (blocked.get("error") or ""), str(blocked.get("error")))
blocked_inv = bc_assets.invite(db, SAT2, "janis@glitchy.ai")
check("inviting into a BC we can't reach is refused, saying the first invite comes from that BC",
      "own login" in (blocked_inv.get("error") or ""), str(blocked_inv.get("error")))
check("the satellite BC stays on the list until it's connected, and can be dropped",
      [w["bc_id"] for w in bc_assets.watchlist(db)] == [SAT2])
with mock.patch.object(tiktok_api, "list_business_centers", side_effect=fake_bcs):
    ready = bc_assets.stage(db, SAT)
check("a BC we are Admin of reads as ready to connect", ready["code"] in ("ready", "connected"), str(ready))
bc_assets.watch_remove(db, SAT2)
check("removing from the list changes nothing on TikTok", bc_assets.watchlist(db) == [] and sent == [])

db.close()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
