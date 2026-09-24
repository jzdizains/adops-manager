"""Launch-failure "fix it here" hints (v143).

A CONFIG failure like "Library creatives support Website / Pixel destinations only" mentions
the word "Pixel" as a destination TYPE, and the hint logic used to match that to the
missing-pixel case and send the operator to the Pixels page — the wrong place. The real fix
is in the preset (change the destination, or use a spark/profile creative). This locks in the
routing so destination/creative-source mismatches point at the preset, while genuine
missing-pixel errors still point at Pixels."""
import os, sys, types, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

spec = importlib.util.spec_from_file_location("error_messages", os.path.join(ROOT, "app", "error_messages.py"))
em = importlib.util.module_from_spec(spec)
spec.loader.exec_module(em)
Log = types.SimpleNamespace

def fix(msg, template_id=42):
    return em.fix_for(Log(error_code="CONFIG", error_message=msg, template_id=template_id,
                          advertiser_id="", spark_code_id=None))

print("-- the library/destination mismatch points at the preset, not Pixels --")
r = fix("Library creatives support Website / Pixel destinations only.")
check("library-creative destination error → Edit the preset", r["label"] == "Edit the preset" and r["href"].startswith("/presets/42/edit"), r)
check("its reason names destination vs creative source, not a missing pixel", "destination" in r["why"].lower() and "pixel" not in r["why"].lower().split("website")[0])
r_sp = fix("Smart+ presets support Website / Pixel destinations only (TikTok's Smart+ web flow).")
check("the Smart+ 'destinations only' error also routes to the preset", r_sp["href"].startswith("/presets/42/edit"))
r_np = fix("Library creatives support Website / Pixel destinations only.", template_id=None)
check("with no preset id it falls back to Open Presets (still not Pixels)", r_np["href"] == "/presets")

print("-- a genuine missing-pixel error still points at Pixels --")
r2 = fix("Conversion campaigns need a pixel + optimization event — pick both in the preset.")
check("missing pixel/event → Open Pixels", r2["label"] == "Open Pixels" and r2["href"] == "/pixels", r2)

print("-- the Edit-the-preset link deep-links to the exact field (?fix=) --")
check("library/destination error rings the destination chooser", fix("Library creatives support Website / Pixel destinations only.")["href"] == "/presets/42/edit?fix=destination")
check("missing landing page rings the landing-page field", fix("Library-creative presets need a landing page URL.")["href"] == "/presets/42/edit?fix=landing_page_url")
check("missing ad text rings the ad-text field", fix("TikTok requires ad text on every ad.")["href"] == "/presets/42/edit?fix=ad_text")
check("wrong creative source rings the creative chooser", fix("set the creative source to “Library” to use it.")["href"] == "/presets/42/edit?fix=creative_source")

print("-- the editor tags those fields and rings + explains the one from ?fix= --")
def rd(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
tf = rd("app/templates/template_form.html")
for k in ("destination", "landing_page_url", "pixel_event", "creative_source", "ad_text"):
    check(f"the preset editor tags the {k} field", f'data-fix="{k}"' in tf)
pb = rd("app/static/preset-builder.js")
check("the editor reads ?fix=, opens that step, rings the field and explains it",
      "match(/[?&]fix=([a-z_]+)/)" in pb and "fix-flash" in pb and "fix-note" in pb and "FIX_WHY" in pb)
css = rd("app/static/style.css")
check("the ring + note styles exist and are token-based (both themes)", ".fix-flash" in css and ".fix-note" in css and "var(--warn)" in css)

print("-- 'Lead Generation agreement not signed' is explained as what it is (v155.13) --")
AGREE = "Lead Generation agreement has not be signed yet."
e = em.explain("40002", AGREE)
check("40002 'agreement not signed' gets a plain-English meaning (not the generic 40002 text)",
      "Lead Generation Terms aren't accepted on this ad account" in e["friendly"] and "per ad account" in e["action"].lower())
check("…says how: the Review step's Accept button, then Retry failed",
      "accept lead generation terms" in e["action"].lower() and "retry failed" in e["action"].lower())
check("it is NOT mistaken for a permission/connection error", e["is_permission"] is False)
r_ag = em.fix_for(Log(error_code="40002", error_message="TikTok rejected a field value.",
                      error_technical=AGREE, template_id=42, advertiser_id="123",
                      spark_code_id=None))
check("the fix button links TikTok's Lead Generation Terms",
      r_ag and "Lead Generation Terms" in r_ag["label"] and r_ag["href"] == "https://ads.tiktok.com/i18n/official/policy/lead-gen-terms")

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
