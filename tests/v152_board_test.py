"""v152 — Campaigns board: lifetime numbers per row, reconciliation lines, Pending oldest
first, Blocked dated by when the error started (errors-since filter)."""
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

bn = importlib.import_module("app.board_numbers")
print("-- lifetime --")
out = bn.apportion({"A": 100, "B": 300, "C": 50, "D": 0}, {"A": "s1", "B": "s1", "C": "s2", "D": ""}, {"s1": 800, "s2": 25})
check("a source shared by two campaigns is split by lifetime spend", out["A"]["revenue"] == 200 and out["B"]["revenue"] == 600)
check("profit and ROAS since launch", out["A"]["profit"] == 100 and out["A"]["roas"] == 2.0 and out["C"]["roas"] == 0.5 and out["C"]["profit"] == -25)
check("no source → no revenue, no ROAS (never a fake 0×)", out["D"] == {"spend": 0.0, "revenue": 0.0, "profit": 0.0, "roas": 0.0, "has": False})
out = bn.apportion({"A": 0, "B": 0}, {"A": "s", "B": "s"}, {"s": 10})
check("nothing spent yet on a shared source → even split", out["A"]["revenue"] == 5 and out["B"]["revenue"] == 5)

print("-- reconciliation --")
pb = {"s1": {"revenue": 50}, "typo-src": {"revenue": 30.5}, "old": {"revenue": 0}, "x": {"revenue": 4}}
tot, items = bn.unmatched(pb, ["s1", "s2", ""])
check("revenue on sources no campaign carries, biggest first; zero rows left out", tot == 34.5 and items == [("typo-src", 30.5), ("x", 4.0)])
check("…nothing unmatched → 0", bn.unmatched({"s1": {"revenue": 9}}, ["s1"]) == (0.0, []))
R = lambda cid, adv, spend, name="c": types.SimpleNamespace(campaign_id=cid, advertiser_id=adv, spend_today=spend, campaign_name=name)
tot, items = bn.outside_spend(None, None, [R("1", "a", 12.5, "manual one"), R("2", "a", 0), R("3", "b", 40, "agency")], "today", "", "", {"a": "Acct A"})
check("spend outside this board (today = the synced spend), biggest first with account names",
      tot == 52.5 and items[0] == {"name": "agency", "account": "b", "spend": 40.0} and items[1]["account"] == "Acct A" and len(items) == 2)

print("-- errors are dated --")
acct = types.SimpleNamespace(status_changed_at=datetime(2026, 9, 1, 8, 0))
h = {"since": datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)}
check("an account-level block dates from the account's status change", bn.error_at(h, acct, True) == datetime(2026, 9, 1, 8, 0))
check("a campaign-level block dates from its ad groups' state (made naive UTC)", bn.error_at(h, acct, False) == datetime(2026, 9, 20, 9, 0))
check("unknown → None (counts as recent, never hidden)", bn.error_at(None, None, False) is None)

st = read("app/routes/status.py")
check("Blocked: only errors that started in the range, the rest counted as 'older' (one click away)",
      'if state == "blocked" and not older and err_at is not None and err_at < start_utc.replace(tzinfo=None):' in st
      and 'state_counts["blocked_older"] += 1' in st and 'older = request.query_params.get("older") == "1"' in st)
check("Pending: longest-waiting first unless a sort was picked", 'if state == "pending" and "sort" not in request.query_params:' in st)
check("lifetime numbers on every row; reconciliation computed (unmatched only on the everything view)",
      'row["life"] = life.get(row["r"].campaign_id)' in st and "if sc.ids is None:" in st.split("reconciliation")[1][:300]
      and "r.campaign_id not in tool_campaign_ids" in st)
t = read("app/templates/status.html")
check("the page shows them", 'class="life' in t and "Spend outside this board" in t and "Revenue not matched to any campaign" in t and "older errors" in t)
check("the reconciliation lives inside the card that refreshes in place", t.index('<div class="recon">') > t.index('id="campCard"'))

print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
