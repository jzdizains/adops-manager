"""Campaign tags (v103): names/colours are cleaned, duplicates fold together,
toggling is idempotent and capped, deleting removes the tag everywhere, and the
page's lookup returns one sorted list per campaign. Runs without sqlalchemy."""
import sys, types, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

class _Col:
    def __eq__(self, o): return ("eq", o)
    def in_(self, v): return ("in", list(v))
    def asc(self): return self
    __hash__ = object.__hash__
sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm"); orm.Session = type("Session", (), {}); sa.orm = orm
sys.modules["sqlalchemy"], sys.modules["sqlalchemy.orm"] = sa, orm
pkg = types.ModuleType("app"); pkg.__path__ = [os.path.join(ROOT, "app")]; sys.modules["app"] = pkg
class Tag:
    name = _Col(); id = _Col()
    def __init__(self, **k): self.__dict__.update({"id": None, "color": "grey"}); self.__dict__.update(k)
class CampaignTag:
    tag_id = _Col(); campaign_id = _Col()
    def __init__(self, **k): self.__dict__.update(k)
models = types.ModuleType("app.models"); models.Tag = Tag; models.CampaignTag = CampaignTag; sys.modules["app.models"] = models
import importlib
tg = importlib.import_module("app.tags")

# a tiny in-memory "database" that understands the few query shapes tags.py uses
class DB:
    def __init__(self): self.tags = []; self.links = []; self._id = 0
    def add(self, r):
        if isinstance(r, Tag): self._id += 1; r.id = self._id; self.tags.append(r)
        else: self.links.append(r)
    def flush(self): pass
    def get(self, model, id): return next((t for t in self.tags if t.id == id), None) if model is Tag else None
    def query(self, *cols):
        db = self
        class Q:
            def __init__(self): self.model = Tag if (cols and (cols[0] is Tag)) else CampaignTag; self.conds = []; self.cols = cols
            def filter(self, *a): self.conds.extend(a); return self
            def filter_by(self, **k): self.conds.extend([("kw", k)]); return self
            def order_by(self, *a): return self
            def _rows(self):
                rows = list(db.tags if self.model is Tag else db.links)
                for c in self.conds:
                    if isinstance(c, tuple) and c[0] == "kw":
                        rows = [r for r in rows if all(getattr(r, a) == v for a, v in c[1].items())]
                    elif isinstance(c, tuple) and c[0] == "eq":
                        rows = [r for r in rows if r.tag_id == c[1]]
                    elif isinstance(c, tuple) and c[0] == "in":
                        rows = [r for r in rows if r.campaign_id in c[1]]
                return rows
            def __iter__(self):
                rows = self._rows()
                if self.model is CampaignTag and self.cols and self.cols[0] is CampaignTag.campaign_id:
                    return iter([(r.campaign_id, r.tag_id) for r in rows])
                return iter(rows)
            def delete(self, **k):
                rows = self._rows()
                if self.model is Tag: db.tags = [t for t in db.tags if t not in rows]
                else: db.links = [l for l in db.links if l not in rows]
                return len(rows)
        return Q()

db = DB()
check("name is trimmed, squashed and capped", tg.clean_name("  no   prelander " + "x" * 60) == ("no prelander " + "x" * 60)[:32])
check("unknown colour → grey", tg.clean_color("neon") == "grey" and tg.clean_color("Blue") == "blue")
t, err = tg.create(db, "Winner", "green")
check("create", not err and t.id == 1 and t.name == "Winner" and t.color == "green")
t2, err = tg.create(db, "winner ", "red")
check("same name (any case) returns the existing tag, colour untouched", not err and t2 is t and t.color == "green")
_, err = tg.create(db, "   ", "green")
check("empty name refused", err == "give the tag a name")
u, err = tg.create(db, "watch", "amber")
_, err2 = tg.update(db, u.id, name="WINNER")
check("rename onto another tag's name refused", "already exists" in err2)
u2, err3 = tg.update(db, u.id, name="watch closely", color="pink")
check("rename + recolour", not err3 and u2.name == "watch closely" and u2.color == "pink")
check("update of a missing tag", tg.update(db, 999, name="x")[1] == "that tag no longer exists")

n, err = tg.set_on(db, ["c1", "c2", "c1"], t.id, True)
check("tag on two campaigns (duplicate id ignored)", not err and n == 2 and len(db.links) == 2)
n, err = tg.set_on(db, ["c1", "c2"], t.id, True)
check("toggling on again changes nothing", n == 0 and len(db.links) == 2)
n, err = tg.set_on(db, ["c3"], 999, True)
check("unknown tag refused", err == "that tag no longer exists")
check("no campaigns refused", tg.set_on(db, [], t.id, True)[1] == "no campaign given")
tg.set_on(db, ["c2"], u.id, True)
m = tg.by_campaign(db, ["c1", "c2", "c9"])
check("lookup: one sorted list per campaign", [x["name"] for x in m["c2"]] == ["watch closely", "Winner"] and [x["name"] for x in m["c1"]] == ["Winner"] and "c9" not in m, str(m))
check("lookup with an empty id list is empty (no query)", tg.by_campaign(db, []) == {})
n, err = tg.set_on(db, ["c1", "c2"], t.id, False)
check("tag off both", n == 2 and all(l.tag_id != t.id for l in db.links))
removed = tg.delete(db, u.id)
check("delete removes the tag and its links", removed == 1 and db.get(Tag, u.id) is None and not db.links)
big = ["c%d" % i for i in range(700)]
n, _ = tg.set_on(db, big, t.id, True)
check("one call tags at most 500 campaigns", n == 500)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
