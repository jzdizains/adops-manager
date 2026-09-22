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
check("library-creative destination error → Edit the preset", r["label"] == "Edit the preset" and r["href"] == "/presets/42/edit", r)
check("its reason names destination vs creative source, not a missing pixel", "destination" in r["why"].lower() and "pixel" not in r["why"].lower().split("website")[0])
r_sp = fix("Smart+ presets support Website / Pixel destinations only (TikTok's Smart+ web flow).")
check("the Smart+ 'destinations only' error also routes to the preset", r_sp["href"] == "/presets/42/edit")
r_np = fix("Library creatives support Website / Pixel destinations only.", template_id=None)
check("with no preset id it falls back to Open Presets (still not Pixels)", r_np["href"] == "/presets")

print("-- a genuine missing-pixel error still points at Pixels --")
r2 = fix("Conversion campaigns need a pixel + optimization event — pick both in the preset.")
check("missing pixel/event → Open Pixels", r2["label"] == "Open Pixels" and r2["href"] == "/pixels", r2)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
