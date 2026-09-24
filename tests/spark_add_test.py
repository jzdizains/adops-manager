"""Add spark codes — structured entry (v133/134).

The "Add spark codes" modal now gives each code its own row: a Name field + one
spark-code field, a ＋ to add more, a ✕ to remove, Enter in a code jumps to the next
row. The paste box / CSV stays behind a toggle for big batches. Structured rows POST
as parallel row_name / row_code fields, so a name is never mistaken for a code and a
code never mistaken for a name — no parsing guesswork.

Functional (fastapi/sqlalchemy stubbed): the paste parser (parse_bulk / parse_bulk_line)
still detects code, type, url, @creator, name, source in any order.
Source/route: /spark-codes/bulk consumes row_code / row_name when present (empty rows
dropped) and only falls back to the text parser otherwise. Template + JS + CSS: the row
UI, the ＋/✕, Enter-to-next, the paste toggle, and the version bump."""
import os, sys, types

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
    def __call__(self, *a, **k): return self
    def __getattr__(self, n): return _Any()

_mod("fastapi", APIRouter=_Any, Depends=_Any(), Request=_Any, Form=_Any())
_mod("fastapi.responses", RedirectResponse=_Any, JSONResponse=_Any)
sa = _mod("sqlalchemy", func=_Any()); _mod("sqlalchemy.orm", Session=_Any); sa.orm = sys.modules["sqlalchemy.orm"]
pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
routes = _mod("app.routes"); routes.__path__ = [os.path.join(ROOT, "app", "routes")]
_mod("app.models"); _mod("app.queries"); _mod("app.tiktok_api", TikTokError=Exception)
_mod("app.database", get_db=lambda: None); _mod("app.templating", render=lambda *a, **k: None)
_mod("app.scope", for_request=lambda *a: None); _mod("app.routes.guard", view=_Any())

import importlib
sp = importlib.import_module("app.routes.spark_codes")

print("-- paste parser still works (the batch path) --")
r = sp.parse_bulk_line("hook v3 | #ohMN6mHyMnI2RNpwXy0GiyzUxeRGabcd | video | https://www.tiktok.com/@bob/video/123 | src-hook")
check("name, code, type, url, source parsed in any order", r and r["name"] == "hook v3" and r["code"].startswith("#ohMN") and r["media_type"] == "VIDEO"
      and r["tiktok_post_url"].endswith("/123") and r["source"] == "src-hook", r)
check("a lone auth code is enough", (sp.parse_bulk_line("#HEO5xA4whgKls1Q8rJOuNp7p2KGPzzzz") or {}).get("code", "").startswith("#HEO5"))
check("photo URL → carousel", (sp.parse_bulk_line("#ABCDEFGH12345678 | https://www.tiktok.com/@x/photo/9") or {}).get("media_type") == "CAROUSEL")
rows, bad = sp.parse_bulk("name | code\n#SD93QJYXLZVH3jCUI09AWIYt2CfBzzzz , carousel\nnonsense words here")
check("parse_bulk skips the header, reads a code line, flags a line with no code", len(rows) == 1 and rows[0]["media_type"] == "CAROUSEL" and len(bad) == 1, (rows, bad))

print("-- route: structured rows are unambiguous, empty rows dropped --")
src = open(os.path.join(ROOT, "app", "routes", "spark_codes.py"), encoding="utf-8").read()
ab = src.split("async def add_bulk")[1]
check("reads parallel row_code / row_name from the form", 'form.getlist("row_code")' in ab and 'form.getlist("row_name")' in ab)
check("uses the structured rows when any code is present; an empty code is skipped",
      "if any(row_codes):" in ab and "if not code:" in ab and "continue" in ab.split("if any(row_codes):")[1][:400])
check("each structured row keeps its own name + code with the shared defaults", '"name": (row_names[i] if i < len(row_names) else "")[:120], "code": code' in ab)
check("still falls back to the paste/CSV parser when no rows are typed", "else:\n        rows, bad = parse_bulk(text, default_media)" in ab)
check("downstream is unchanged: dedup, group, default source still apply", 'row = models.SparkCode(name=r["name"] or r["code"][:12]' in src and 'source=r["source"] or default_source' in src)

print("-- template + JS + CSS --")
t = open(os.path.join(ROOT, "app", "templates", "spark_codes.html"), encoding="utf-8").read()
check("one row per code: a name input + a single code input + a remove button", 'name="row_name"' in t and 'name="row_code"' in t and 'class="sp-in-x"' in t and 'id="spRows"' in t)
check("＋ Add another + a paste/CSV toggle (batch path preserved, not removed)", 'id="spAddRow"' in t and 'id="spPasteToggle"' in t and 'id="spPasteBox"' in t and 'name="lines"' in t and 'type="file"' in t)
check("shared Type / Creator / Source fields kept", 'name="media_type"' in t and 'name="group_name"' in t and 'name="source"' in t)
check("JS: add a row, remove a row (never below one), Enter in a code jumps to the next",
      "function addRow(" in t and 'e.target.id === "spAddRow"' in t and "rows.length > 1" in t
      and 'classList.contains("sp-in-code")' in t and "next = addRow(false)" in t)
check("JS: Add all submits the rows or the paste box; empty → focus, not a blank post", "var hasRow = " in t and "var hasPaste = " in t and "node.submit();" in t)
css = open(os.path.join(ROOT, "app", "static", "style.css"), encoding="utf-8").read()
check("CSS for the rows + STATIC_VERSION bumped", ".sp-row-in" in css and ".sp-in-code" in css and 'STATIC_VERSION = "165"' in open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read())

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
