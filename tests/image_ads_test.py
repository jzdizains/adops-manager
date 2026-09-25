"""v155.24 — single-image ads from the Super Launcher: the library picker's Images tab, one
SINGLE_IMAGE ad per picked image (uploaded into each account like a carousel slide), the
preset's text / CTA / destination, no music, no display card; Smart+ and Smart Creative refused
up front like carousels. Payload builders are pure; the rest is source-checked."""
import os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

src = read("app/routes/campaigns.py")
def grab(name):
    i = src.index("def " + name + "(")
    j = src.index("\ndef ", i + 1)
    return src[i:j]
ns = {"_cta": lambda f: {"call_to_action": f.get("call_to_action") or "LEARN_MORE"}, "models": types.SimpleNamespace(SparkCode=object)}
exec(grab("apply_destination"), ns)
exec(grab("build_image_ad_payload"), ns)
exec(grab("build_carousel_ad_payload"), ns)
IDENT = {"identity_id": "I1", "identity_type": "CUSTOMIZED_USER", "identity_authorized_bc_id": "BC1"}

print("-- the image ad payload --")
f = {"template_name": "JanisLeadCR", "ad_text": "shop now", "destination_type": "website", "landing_page_url": "https://offer.example/x", "call_to_action": "SHOP_NOW"}
p = ns["build_image_ad_payload"](f, "AG1", IDENT, "IMG1")
c = p["creatives"][0]
check("without music: SINGLE_IMAGE with exactly one image_id, the preset's text and CTA, the account identity",
      c["ad_format"] == "SINGLE_IMAGE" and c["image_ids"] == ["IMG1"] and c["ad_text"] == "shop now" and c["call_to_action"] == "SHOP_NOW"
      and c["identity_id"] == "I1" and c["identity_authorized_bc_id"] == "BC1" and p["adgroup_id"] == "AG1", str(c))
check("no music, no video, no display card on an image ad", not any(k in c for k in ("music_id", "video_id", "card_id", "image_id")))
ph = ns["build_image_ad_payload"](f, "AG1", IDENT, "IMG1", "MUSIC7")["creatives"][0]
check("with a music track: a PHOTO post — CAROUSEL_ADS with one image and that track (what Ads Manager makes of a single image)",
      ph["ad_format"] == "CAROUSEL_ADS" and ph["image_ids"] == ["IMG1"] and ph["music_id"] == "MUSIC7" and ph["ad_text"] == "shop now")
check("website → landing page URL", c.get("landing_page_url") == "https://offer.example/x")
lf = ns["build_image_ad_payload"]({**f, "destination_type": "lead_form", "lead_form_id": "FORM1", "landing_page_url": ""}, "AG1", IDENT, "IMG1")["creatives"][0]
check("Instant Form → page_id (like every other creative source)", lf.get("page_id") == "FORM1" and "landing_page_url" not in lf)
check("the ad name says image; empty text still sends a space (TikTok requires the field)",
      c["ad_name"].endswith(" image") and ns["build_image_ad_payload"]({**f, "ad_text": ""}, "AG1", IDENT, "I")["creatives"][0]["ad_text"] == " ")

print("-- wiring --")
check("creative_source 'image' is a library source everywhere spark-vs-library is decided",
      'LIBRARY_SOURCES = ("library", "carousel", "image")' in src and src.count("LIBRARY_SOURCES") >= 5
      and 'SOURCE_OF_KIND = {"carousel": "carousel", "image": "image"}' in src and src.count("SOURCE_OF_KIND.get(") == 3)
check("the launch: an image rides the carousel path with one slide and no music / slide-count checks",
      'use_image = fields.get("creative_source") == "image"' in src and 'use_carousel = fields.get("creative_source") == "carousel" or use_image' in src
      and "slides = [carousel]" in src and 'want_kind = "image" if use_image else "carousel"' in src
      and src.index("if use_image:\n                if not carousel.file_path") < src.index("slides = carousel_slides(db, carousel)"))
check("…objective rule stays carousel-only; Smart+ / Smart Creative refused for both, in words",
      'if not use_image and fields["objective_type"] not in CAROUSEL_OBJECTIVES' in src and 'noun = "Image ads" if use_image else "Carousel Ads"' in src)
check("…the ad goes out as a photo post (one image + music); there is NO plain-image fallback (TikTok: 'Incorrect source field', live 25 Sep)",
      'if carousel is not None and carousel.kind == "image":' in src and "build_image_ad_payload(fields, adgroup_id, ident, carousel_image_ids[0], image_music_id)" in src
      and '"image-photo-refused"' not in src and 'build_image_ad_payload(fields, adgroup_id, ident, carousel_image_ids[0], "")' not in src)
check("the track: the image's own; TikTok's recommendation (TWO urls — the endpoint's minimum); the Commercial Music Library; history; a carousel-confirmed one",
      "def image_music(" in src and 'image_urls=[image_url, image_url]' in src and '"SEARCH_BY_SOURCE", sources=["SYSTEM"]' in src and '"SEARCH_BY_HISTORY"' in src
      and "MusicTrack.carousel_ok == True" in src and "image_music_id = image_music(db, acct, carousel, uploads[0][1])" in src)
check("no track at all → the launch stops BEFORE creating the campaign, in words, with the way out",
      "if not image_music_id:\n                    raise ConfigError(" in src and src.index("if not image_music_id:") < src.index('trace.inflight("Smart+ campaign")'))
check("the image is uploaded into each account through the cached slide upload (delivery size)",
      "uploads = [_upload_image_to_account(db, acct, img) for img in slides]" in src and "carousel_image_ids = [u[0] for u in uploads]" in src)
check("Review: no display card on image ads either", 'in ("carousel", "image")' in read("app/launch_review.py"))
sl = read("app/templates/super_launcher.html")
check("Super Launcher picker: Videos, Carousels AND Images; images board as images, not videos",
      'kinds: ["video", "carousel", "image"]' in sl and 'img = c.kind === "image"' in sl and '(car || img) ? ""' in sl and "addItems(res.map(fromLibrary))" in sl)
pk = read("app/static/picker.js")
check("the picker keeps a mixed multi-selection across its tabs", 'if (!o.multi) { sel = {}; order = []; } load();' in pk and 'data-kind="image">Images' in pk)
print("---")
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
