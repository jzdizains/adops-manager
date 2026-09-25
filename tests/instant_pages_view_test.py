"""Instant Pages redesign (v132): one row per page NAME.

Pure model (app/instant_pages_view.py, no DB):
  · rows folded per name: copies, published/draft counts, coverage, per-BC missing
  · favourites first, then A–Z; the workspace's marks applied; a deleted tag ignored
  · the clone source is the first PUBLISHED copy (none → no source → button disabled)
  · page_json carries no server-only fields
Route (source): the mark endpoint upserts per (workspace, name), validates the tag is in
view, answers JSON, and never 500s on a busy DB. Template: rows per group, search bar,
favourite / BC / status / coverage filters, tag chips, JSON blob, no hidden per-row
account <select>s (the old page shipped ~70,000 of them)."""
import json, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]; sys.modules["app"] = pkg
import importlib
ipv = importlib.import_module("app.instant_pages_view")

class P:
    def __init__(self, name, page_id, adv, status="PUBLISHED", preview=""):
        self.name, self.page_id, self.owner_advertiser_id, self.status, self.preview_url = name, page_id, adv, status, preview
class A:
    def __init__(self, i, name, bc):
        self.advertiser_id, self.advertiser_name, self.owner_bc_id = i, name, bc

accounts = [A("a1", "Acct One", "bc1"), A("a2", "Acct Two", "bc1"), A("a3", "Acct Three", "bc2"), A("a4", "Loose", "")]
pages = [P("Benjamin", "p1", "a1", preview="https://x/1"), P("Benjamin", "p2", "a2"), P("Benjamin", "p3", "a3", status="EDITED"),
         P("Zeta", "p9", "a1", status="EDITED"), P("Alpha", "p5", "a4")]
tags = {7: {"id": 7, "name": "UK", "color": "blue"}, 8: {"id": 8, "name": "cash", "color": "green"}}
marks = {"Zeta": {"favorite": True, "tag_ids": [7, 99]}, "Benjamin": {"favorite": False, "tag_ids": [8]}}
out = ipv.group_pages(pages, accounts, {"bc1": "Blue Bat", "bc2": "BC 60"}, marks, tags)
g = {x["name"]: x for x in out["groups"]}

print("-- grouping --")
check("one row per name, favourites first then A–Z", [x["name"] for x in out["groups"]] == ["Zeta", "Alpha", "Benjamin"], [x["name"] for x in out["groups"]])
b = g["Benjamin"]
check("counts: 3 copies, 2 published, 1 draft; 2 Business Centers", b["count"] == 3 and b["published"] == 2 and b["drafts"] == 1 and b["bc_count"] == 2, b)
check("clone source = the first PUBLISHED copy; a draft-only page has none", b["source"] == {"page_id": "p1", "adv": "a1"} and g["Zeta"]["source"] is None)
check("missing per BC: Benjamin lacks nobody in bc1, nobody in bc2, the loose account", b["missing_by_bc"] == {"bc1": 0, "bc2": 0, "": 1}, b["missing_by_bc"])
check("Alpha (only on the loose account) is missing on both BCs", g["Alpha"]["missing_by_bc"] == {"bc1": 2, "bc2": 1, "": 0}, g["Alpha"]["missing_by_bc"])
check("marks applied; a tag id that no longer exists (99) is dropped", g["Zeta"]["favorite"] is True and [t["id"] for t in g["Zeta"]["tags"]] == [7] and [t["id"] for t in b["tags"]] == [8])
check("copies carry page id, status, preview and the account's BC", b["copies"][0] == {"adv": "a1", "page_id": "p1", "status": "PUBLISHED", "preview": "https://x/1", "bc": "bc1"}, b["copies"][0])
check("bcs: named, sized, 'No Business Center' last", [(x["bc_id"], x["name"], x["n"]) for x in out["bcs"]] == [("bc2", "BC 60", 1), ("bc1", "Blue Bat", 2), ("", "No Business Center", 1)], out["bcs"])
check("total = accounts in view", out["total"] == 4)
pj = ipv.page_json(out)
check("page_json: groups/accounts/bcs/total, JSON-serialisable, no 'have' set", set(pj) == {"total", "accounts", "bcs", "groups"} and all("have" not in x for x in pj["groups"]) and json.dumps(pj))
check("parse_tag_ids: tolerant", ipv.parse_tag_ids('[7, "8", 7, "x"]') == [7, 8] and ipv.parse_tag_ids("garbage") == [] and ipv.parse_tag_ids(None) == [])

print("-- route + template (source) --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
r = read("app/routes/instant_pages.py"); t = read("app/templates/instant_pages.html"); m = read("app/models.py"); css = read("app/static/style.css")
check("model: InstantPageMark unique per (workspace, name), tags as JSON", "class InstantPageMark(Base)" in m and 'UniqueConstraint("owner_user_id", "page_name", name="uq_ip_mark")' in m and 'tag_ids = Column(Text, default="[]")' in m)
mk = r.split('@router.post("/instant-pages/mark")')[1]
check("mark route: upsert per workspace + name, tag must be in view, JSON answer, busy DB → 503 not 500",
      "models.InstantPageMark.owner_user_id == owner, models.InstantPageMark.page_name == name" in mk and "tags_mod.get_in_view(db, tid, sc.user_id) is None" in mk
      and "if not safe_commit(db):" in mk and "status_code=503" in mk and '"favorite": bool(row.favorite)' in mk)
check("marks owner: the workspace in view, else the logged-in user", "return sc.user_id if sc.user_id is not None else sc.me_id" in r)
check("page route groups through instant_pages_view and ships the JSON", "ipv.group_pages(pages, accounts, bc_names, _marks_for(db, owner)" in r and '"page_data": ipv.page_json(grouped)' in r)
check("template: one row per group with star, tags, coverage bar, Accounts + Clone", 'class="ip-row" data-name="{{ g.name }}"' in t and 'class="ip-star' in t and '<span class="ctags">' in t and 'class="cov-bar"' in t and 'class="btn sm ip-accounts"' in t and 'class="btn sm ip-clone"' in t)
check("template: search + favourites + BC / status / coverage filters + tag chips", 'id="ipSearch"' in t and 'id="ipFav"' in t and 'id="ipBc"' in t and 'id="ipStatus"' in t and 'id="ipCov"' in t and 'id="ipTags"' in t)
check("template: JSON blob for the page script; no hidden per-row account <select>s any more", 'id="ipData"' in t and "{{ page_data|tojson }}" in t and 'name="to_advertiser_id"' not in t and "{% for a in accounts %}{% if a.advertiser_id != p.owner_advertiser_id %}" not in t)
check("template: templates card collapsible, help text in a popover, page JS included", "<details class=\"card tpl-card\"" in t and 'data-expand-title="How Sync and Clone work"' in t and "/static/instant-pages.js?v={{ STATIC_V }}" in t)
check("CSS for the new pieces + STATIC_VERSION bumped", ".ip-star.on" in css and ".cov-bar" in css and ".ipd-row" in css and 'STATIC_VERSION = "176"' in read("app/config.py"))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
