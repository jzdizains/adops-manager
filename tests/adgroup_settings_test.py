"""Drawer → ad group "Settings" popover (v111): what TikTok has stored on an ad group.

- adgroup_view.rows(): known fields first with plain labels, empty ones skipped, every
  other non-empty field TikTok returned appended (marked other), values formatted.
- the endpoint + JS wiring exist and the JS never opens the drawer (row click) by mistake.
Runs without any dependency."""
import os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]; sys.modules["app"] = pkg
import importlib
av = importlib.import_module("app.adgroup_view")

# the live Ads-Manager Engaged-session ad group, as /adgroup/get/ returned it (trimmed)
g = {"adgroup_id": "1876342860908641", "adgroup_name": "TrafficSpark", "campaign_id": "1876342893366658",
     "optimization_goal": "ENGAGEMENT_SESSION", "optimization_event": "", "pixel_id": None, "billing_event": "OCPM",
     "bid_type": "BID_TYPE_NO_BID", "bid_price": 0, "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET", "budget": 10000,
     "placements": ["PLACEMENT_TIKTOK"], "placement_type": "PLACEMENT_TYPE_NORMAL", "location_ids": ["6252001"],
     "age_groups": ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54"], "gender": "GENDER_UNLIMITED",
     "dayparting": "1" * 336, "comment_disabled": False, "share_disabled": True, "promotion_website_type": "UNSET",
     "operation_status": "ENABLE", "secondary_status": "ADGROUP_STATUS_AUDIT", "create_time": "2026-09-14 21:10:55",
     "some_new_tiktok_field": "NEW_VALUE", "interest_category_ids": [str(i) for i in range(12)]}
rows = av.rows(g)
by = {r["key"]: r for r in rows}
check("known fields come first, in Ads-Manager order", [r["key"] for r in rows[:3]] == ["optimization_goal", "billing_event", "bid_type"], str([r["key"] for r in rows[:5]]))
check("the goal reads as TikTok stores it", by["optimization_goal"]["value"] == "ENGAGEMENT_SESSION" and by["optimization_goal"]["label"] == "Optimisation goal")
check("empty / None / UNSET / 0-bid fields are skipped", "optimization_event" not in by and "pixel_id" not in by and "promotion_website_type" not in by)
check("bid 0 is shown (it is a value)", by["bid_price"]["value"] == "0")
check("booleans read on/off; False still shown", by["share_disabled"]["value"] == "on" and by["comment_disabled"]["value"] == "off")
check("all-hours dayparting summarised", by["dayparting"]["value"] == "all hours")
check("a custom dayparting is counted", av._fmt("dayparting", "1" * 100 + "0" * 236) == "custom (100 of 336 half-hours)")
check("long lists are capped with a count", by["interest_category_ids"]["value"].endswith("… +4  (12)"), by["interest_category_ids"]["value"])
check("short lists are joined", by["placements"]["value"] == "PLACEMENT_TIKTOK")
check("identity fields (ids, names, times, statuses) are not repeated", not any(k in by for k in ("adgroup_id", "campaign_id", "create_time", "operation_status", "secondary_status")))
check("a field TikTok added that we don't know is still shown, flagged as other", by["some_new_tiktok_field"]["other"] is True and by["some_new_tiktok_field"]["value"] == "NEW_VALUE" and rows[-1]["key"] == "some_new_tiktok_field")
check("no other flag on known rows", all(not r.get("other") for r in rows if r["key"] != "some_new_tiktok_field"))
check("nothing crashes on an empty object", av.rows({}) == [])

st = open(os.path.join(ROOT, "app", "routes", "status.py"), encoding="utf-8").read()
check("endpoint exists and is read-only (one /adgroup/get/ via _source_adgroup, no write)",
      '/adgroups/{adgroup_id}/settings.json' in st and "adgroup_copy._source_adgroup(acct, campaign_id, adgroup_id)" in st
      and "db.commit" not in st[st.index('/adgroups/{adgroup_id}/settings.json'):st.index('@router.post("/campaigns/{advertiser_id}/{campaign_id}/agmode")')])
js = open(os.path.join(ROOT, "app", "static", "campaigns.js"), encoding="utf-8").read()
check("drawer row has a Settings button", 'class="btn sm dw-ag-set"' in js)
check("the button opens a popover from the endpoint", '"/adgroups/" + row.dataset.ag + "/settings.json"' in js and "UI.popover(btn, html, {})" in js)
check("its click never bubbles into the row-click handler", 'closest(".dw-ag-set");\n    if (b) { e.preventDefault(); e.stopImmediatePropagation();' in js)
check("values are escaped (TikTok strings land in HTML)", "esc(r.value)" in js and "esc(r.label)" in js)
css = open(os.path.join(ROOT, "app", "static", "style.css"), encoding="utf-8").read()
check("popover styles present", ".agset-grid" in css and ".agset .tp-sep" in css)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
