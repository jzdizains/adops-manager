"""Library (uploaded) video → Instant Form / Instant Page (v144).

TikTok DOES allow an uploaded video with an Instant Form on a Lead-Gen campaign (and with
an Instant Page) — not only spark posts. The dashboard used to block it because
build_library_ad_payload hardcoded a landing_page_url and never set the page_id. Now a
shared apply_destination() attaches the per-account page_id for instant_page / lead_form
(exactly as the spark path does), and the pre-launch guard allows those destinations.

The payload builders are pure — extracted and unit-run with light stubs — plus a source
assert on the relaxed guard."""
import os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

src = open(os.path.join(ROOT, "app", "routes", "campaigns.py"), encoding="utf-8").read()
def grab(name):
    i = src.index("def " + name + "(")
    j = src.index("\ndef ", i + 1)
    return src[i:j]

ns = {"_cta": lambda f: {"call_to_action": "LEARN_MORE"}, "spark_ad_format": lambda a, b: "SINGLE_VIDEO",
      "models": types.SimpleNamespace(SparkCode=object)}
exec(grab("apply_destination"), ns)
exec(grab("build_ad_payload"), ns)
exec(grab("build_library_ad_payload"), ns)

IDENT = {"identity_id": "I1", "identity_type": "CUSTOMIZED_USER"}
def lib(dest, **extra):
    f = {"template_name": "T", "ad_text": "hi", "destination_type": dest, "landing_page_url": "", **extra}
    return ns["build_library_ad_payload"](f, "AG1", IDENT, "VID1", "COV1")["creatives"][0]

print("-- an uploaded video now attaches the Instant Form / Instant Page --")
lf = lib("lead_form", lead_form_id="FORM1")
check("lead-gen: the library ad carries the form's page_id, no landing page", lf.get("page_id") == "FORM1" and "landing_page_url" not in lf)
check("it's still a real uploaded-video ad (video_id + account identity kept)", lf.get("video_id") == "VID1" and lf.get("identity_id") == "I1" and lf.get("ad_format") == "SINGLE_VIDEO")
ip = lib("instant_page", instant_page_id="PAGE9")
check("instant page: the library ad carries the page_id", ip.get("page_id") == "PAGE9" and "landing_page_url" not in ip)

print("-- website still uses the landing page URL (unchanged) --")
web = lib("website", landing_page_url="https://offer.example/x")
check("website: landing_page_url, no page_id", web.get("landing_page_url") == "https://offer.example/x" and "page_id" not in web)

print("-- the shared helper does the same for the spark path (no regression) --")
spark_ad = ns["build_ad_payload"]({"template_name": "T", "ad_text": "hi", "destination_type": "lead_form", "lead_form_id": "FORM2", "landing_page_url": ""},
                                  "AG1", {"identity_id": "S1", "identity_type": "BC_AUTH_TT", "item_id": "IT1"}, None)["creatives"][0]
check("spark lead-gen still sets the form page_id via the same helper", spark_ad.get("page_id") == "FORM2")

print("-- the pre-launch guard now allows those destinations for library creatives --")
check("guard allows website/pixel/instant_page/lead_form for library",
      'if fields["destination_type"] not in ("website", "pixel", "instant_page", "lead_form"):' in src)
check("a landing page URL is only demanded for website/pixel now",
      'if fields["destination_type"] in ("website", "pixel") and not fields.get("landing_page_url"):' in src)
check("the old 'Website / Pixel destinations only' block is gone", "Library creatives support Website / Pixel destinations only." not in src)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
