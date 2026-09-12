"""Duplicating an ad group.

TikTok has no copy endpoint, so a duplicate is a read followed by a create — and
/adgroup/get/ returns a pile of fields /adgroup/create/ refuses. The payload is therefore
built from an allowlist of what create accepts. These tests pin the two things that go
wrong quietly: a read-only field sneaking into the create, and the copy's ads going missing.

Runs without fastapi/sqlalchemy.
"""
import sys, types, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

if "sqlalchemy" not in sys.modules:
    sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm")
    class Session: pass
    orm.Session = Session; sa.orm = orm
    sys.modules["sqlalchemy"], sys.modules["sqlalchemy.orm"] = sa, orm

pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
sys.modules["app"] = pkg
models = types.ModuleType("app.models")
class AdAccount:
    def __init__(self): self.advertiser_id, self.access_token = "ADV1", "tok"
models.AdAccount = AdAccount
sys.modules["app.models"] = models
tiktok_api = types.ModuleType("app.tiktok_api")
class TikTokError(Exception):
    def __init__(self, code=40002, message="boom", request_id="", data=None, path=""):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message
tiktok_api.TikTokError = TikTokError
sys.modules["app.tiktok_api"] = tiktok_api

import importlib
ac = importlib.import_module("app.adgroup_copy")

# a source ad group as /adgroup/get/ really returns it: real settings PLUS read-only noise
SOURCE = {
    "adgroup_id": "AG1", "campaign_id": "C1", "adgroup_name": "Cold US 0.03",
    "advertiser_id": "ADV1", "create_time": "2026-09-01 10:00:00",
    "modify_time": "2026-09-02 11:00:00", "secondary_status": "ADGROUP_STATUS_DELIVERY_OK",
    "is_new_structure": True, "campaign_name": "Camp", "statistic_type": "",
    "budget": 25.0, "budget_mode": "BUDGET_MODE_DAY", "bid_price": 0.03,
    "billing_event": "OCPM", "optimization_goal": "CONVERT", "optimization_event": "ON_WEB_ORDER",
    "pacing": "PACING_MODE_SMOOTH", "schedule_type": "SCHEDULE_FROM_NOW",
    "schedule_start_time": "2026-09-01 10:00:00", "location_ids": ["6252001"],
    "placements": ["PLACEMENT_TIKTOK"], "placement_type": "PLACEMENT_TYPE_NORMAL",
    "pixel_id": "PX1", "promotion_type": "WEBSITE", "operation_status": "ENABLE",
    "network_types": [], "age_groups": None,
}
ADS = [
    {"ad_id": "AD1", "adgroup_id": "AG1", "advertiser_id": "ADV1", "ad_name": "slides v1",
     "ad_format": "CAROUSEL_ADS", "ad_text": "if you know you know", "image_ids": ["I1", "I2"],
     "music_id": "M1", "identity_id": "ID1", "identity_type": "BC_AUTH_TT",
     "identity_authorized_bc_id": "BC1", "landing_page_url": "https://example.com",
     "call_to_action_id": "CTA1",
     "create_time": "2026-09-01", "secondary_status": "AD_STATUS_DELIVERY_OK",
     "is_aco": False, "ad_texts": None},
]

sent = {"adgroups": [], "ads": []}
def fake_adgroups(token, adv, campaign_ids=None, page=1, page_size=100):
    return {"list": [SOURCE]}
def fake_ads(token, adv, page=1, page_size=100, filtering=None):
    return {"list": [dict(a) for a in ADS], "page_info": {"total_page": 1}}
def fake_create_adgroup(token, adv, payload):
    sent["adgroups"].append(payload)
    return {"adgroup_id": "NEW%d" % len(sent["adgroups"])}
def fake_create_ad(token, adv, payload):
    sent["ads"].append(payload)
    return {"ad_ids": ["NEWAD"]}
tiktok_api.list_adgroups = fake_adgroups
tiktok_api.list_ads = fake_ads
tiktok_api.create_adgroup = fake_create_adgroup
tiktok_api.create_ad = fake_create_ad

acct = AdAccount()

print("\n-- the copy carries the settings and none of the read-only noise --")
rep = ac.duplicate(None, acct, "C1", "AG1", copies=1)
p = sent["adgroups"][0]
for dead in ("adgroup_id", "create_time", "modify_time", "secondary_status",
             "is_new_structure", "campaign_name", "advertiser_id"):
    check(f"“{dead}” is not sent to create", dead not in p, str(sorted(p)))
for kept in ("budget", "budget_mode", "bid_price", "billing_event", "optimization_goal",
             "optimization_event", "pacing", "location_ids", "placements", "pixel_id",
             "promotion_type", "schedule_type"):
    check(f"“{kept}” is carried over", p.get(kept) == SOURCE[kept], f"{kept}={p.get(kept)!r}")
check("it stays in the same campaign", p["campaign_id"] == "C1", str(p.get("campaign_id")))
check("empty values are left out, not sent as empty",
      "network_types" not in p and "age_groups" not in p, str(sorted(p)))
check("the copy is named after the original", p["adgroup_name"] == "Cold US 0.03 (copy)",
      p["adgroup_name"])

print("\n-- the ads come with it, also allowlisted --")
check("one ad was created", len(sent["ads"]) == 1, str(len(sent["ads"])))
c = sent["ads"][0]["creatives"][0]
check("into the NEW ad group", sent["ads"][0]["adgroup_id"] == "NEW1", str(sent["ads"][0]))
check("the creative survives", c["image_ids"] == ["I1", "I2"] and c["music_id"] == "M1", str(c))
check("the identity and its BC survive",
      c["identity_id"] == "ID1" and c["identity_authorized_bc_id"] == "BC1", str(c))
check("the landing page survives", c["landing_page_url"] == "https://example.com", str(c))
for dead in ("ad_id", "create_time", "secondary_status", "is_aco", "advertiser_id", "adgroup_id"):
    check(f"ad's “{dead}” is not sent", dead not in c, str(sorted(c)))
check("the report counts the ads", rep["made"][0]["ads"] == 1, str(rep["made"]))

print("\n-- several copies are numbered and all get the ads --")
sent["adgroups"].clear(); sent["ads"].clear()
rep = ac.duplicate(None, acct, "C1", "AG1", copies=3)
check("three ad groups", len(sent["adgroups"]) == 3, str(len(sent["adgroups"])))
check("named copy 1..3", [a["adgroup_name"] for a in sent["adgroups"]]
      == ["Cold US 0.03 (copy)", "Cold US 0.03 (copy 2)", "Cold US 0.03 (copy 3)"],
      str([a["adgroup_name"] for a in sent["adgroups"]]))
check("three ads, one per copy", len(sent["ads"]) == 3, str(len(sent["ads"])))
check("the summary says it plainly", "3 of 3" in rep["summary"], rep["summary"])
check("and warns that they can spend", "can spend" in rep["summary"], rep["summary"])

print("\n-- one refused copy does not hide the ones that worked --")
sent["adgroups"].clear(); sent["ads"].clear()
calls = {"n": 0}
def flaky(token, adv, payload):
    calls["n"] += 1
    if calls["n"] == 2:
        raise TikTokError(40002, "Budget is below the minimum.")
    sent["adgroups"].append(payload)
    return {"adgroup_id": "NEW%d" % calls["n"]}
tiktok_api.create_adgroup = flaky
rep = ac.duplicate(None, acct, "C1", "AG1", copies=3)
check("two were created", len(sent["adgroups"]) == 2, str(len(sent["adgroups"])))
check("the failure is reported", any("Budget is below" in e for e in rep["errors"]), str(rep["errors"]))
check("the summary counts only what worked", "2 of 3" in rep["summary"], rep["summary"])
tiktok_api.create_adgroup = fake_create_adgroup

print("\n-- a start time in the past is retried, not failed --")
sent["adgroups"].clear()
tries = {"n": 0}
def past_start(token, adv, payload):
    tries["n"] += 1
    if tries["n"] == 1:
        raise TikTokError(40002, "schedule_start_time must be later than the current time")
    sent["adgroups"].append(payload)
    return {"adgroup_id": "NEW1"}
tiktok_api.create_adgroup = past_start
rep = ac.duplicate(None, acct, "C1", "AG1", copies=1)
check("it retried with a future start", len(sent["adgroups"]) == 1, str(tries))
check("and the start time moved", sent["adgroups"][0]["schedule_start_time"]
      != SOURCE["schedule_start_time"], sent["adgroups"][0]["schedule_start_time"])
check("the copy says why", "start time" in (rep["made"][0].get("note") or ""),
      str(rep["made"][0].get("note")))
tiktok_api.create_adgroup = fake_create_adgroup

print("\n-- one click cannot create a hundred live ad groups --")
sent["adgroups"].clear()
rep = ac.duplicate(None, acct, "C1", "AG1", copies=999)
check("copies are capped", len(sent["adgroups"]) == ac.MAX_COPIES, str(len(sent["adgroups"])))
check("the cap is a sane number", ac.MAX_COPIES <= 20, str(ac.MAX_COPIES))

print("\n-- a missing source is refused, not guessed at --")
tiktok_api.list_adgroups = lambda *a, **k: {"list": []}
rep = ac.duplicate(None, acct, "C1", "GONE", copies=1)
check("it says so", "not in this campaign" in (rep.get("error") or ""), str(rep.get("error")))
tiktok_api.list_adgroups = fake_adgroups

print()
print(("FAILED: " + ", ".join(fails)) if fails else "all good")
sys.exit(1 if fails else 0)
