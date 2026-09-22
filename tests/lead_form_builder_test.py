"""Lead-form builder: field-rewrite engine + build orchestration (v137).

The rewrite maps the operator's key fields onto the exact bricks of a real TikTok
instant-form definition (read from form 7688359276615762197). This test uses a faithful
fixture of that structure and proves each field lands on the right brick — and that the
non-lead (disqualified) thank-you page and CTA are left untouched — so the builder edits
by mapping, never by guessing. Also structure-tests build_form's read→create→update→
verify→publish flow with the web layer stubbed."""
import os, sys, types, json, copy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

# a faithful slice of the real form's `data` tree (brick names + fields as TikTok returns them)
FORM = {"data": {
    "meta": {"name": "LpPage", "brickIndex": "meta", "children": ["virtualForm"]},
    "customQuestionClaim": {"name": "LpClaim", "brickIndex": "customQuestionClaim", "content": {"type": "text", "text": "Question"}},
    "47135b5c6df77ad6": {"name": "LpMultipleChoiceField", "brickIndex": "47135b5c6df77ad6", "label": "Are you 18+? ",
                         "options": [{"key": "9daa2ad3f6112e54", "label": "Yes"}, {"key": "00f0642c8dc3c5c5", "label": "No"}]},
    "customQuestionPrivacy": {"name": "LpAgreement", "brickIndex": "customQuestionPrivacy", "companyName": "lbvl",
                              "linkList": [{"linkText": "View lbvl’s privacy policy.", "linkUrl": "https://lovable.dev/privacy", "type": 1}]},
    "reviewPrivacy": {"name": "LpAgreement", "brickIndex": "reviewPrivacy", "companyName": "lbvl",
                      "linkList": [{"linkText": "View lbvl’s privacy policy.", "linkUrl": "https://lovable.dev/privacy", "type": 1}]},
    "thanksContentContainer": {"name": "LpThanksPage", "brickIndex": "thanksContentContainer",
                               "title": "Thanks for your response!", "description": "Tap the button below to start", "ctaCount": 1},
    "nonLeadThanksContentContainer": {"name": "LpThanksPage", "brickIndex": "nonLeadThanksContentContainer",
                                      "title": "Thank you for your interest!", "description": "This might not be the best solution for you.", "ctaCount": 0},
    "leadFormCTA": {"name": "LpFormCTA", "brickIndex": "leadFormCTA", "buttonsOrder": ["website"],
                    "buttonsInfo": {"website": {"title": "Start here", "link": {"linkType": "url", "url": "https://start.thetopgamesvault.com/old/?src=__CSITE__"}}}},
    "nonLeadFormCTA": {"name": "LpFormCTA", "brickIndex": "nonLeadFormCTA", "buttonsOrder": ["website"],
                       "buttonsInfo": {"website": {"title": "View Website", "link": {"linkType": "url", "url": ""}}}},
}}

import importlib
lfb = importlib.import_module("app.lead_form_builder")

print("-- rewrite maps each key field onto the right brick --")
edits = {
    "privacy_url": "https://mysite.com/privacy", "company_name": "Acme",
    "thanks_title": "You're in!", "thanks_description": "Tap below.",
    "destination_url": "https://offer.example/go?x=1", "cta_title": "Claim now",
    "question_label": "Confirm you are 18 or older", "question_options": ["Yes I am", "No"],
}
out_str, changed = lfb.rewrite_form_fields(json.dumps(FORM), edits)
d = json.loads(out_str)["data"]

check("all six LpAgreement/privacy fields point at the new URL",
      d["customQuestionPrivacy"]["linkList"][0]["linkUrl"] == "https://mysite.com/privacy"
      and d["reviewPrivacy"]["linkList"][0]["linkUrl"] == "https://mysite.com/privacy")
check("privacy company name + link text updated",
      d["customQuestionPrivacy"]["companyName"] == "Acme"
      and d["customQuestionPrivacy"]["linkList"][0]["linkText"] == "View Acme’s privacy policy.")
check("the LEAD thank-you screen is updated",
      d["thanksContentContainer"]["title"] == "You're in!" and d["thanksContentContainer"]["description"] == "Tap below.")
check("the NON-lead thank-you screen is LEFT UNTOUCHED",
      d["nonLeadThanksContentContainer"]["title"] == "Thank you for your interest!")
check("the LEAD CTA destination link + label are updated",
      d["leadFormCTA"]["buttonsInfo"]["website"]["link"]["url"] == "https://offer.example/go?x=1"
      and d["leadFormCTA"]["buttonsInfo"]["website"]["title"] == "Claim now")
check("the NON-lead CTA is LEFT UNTOUCHED",
      d["nonLeadFormCTA"]["buttonsInfo"]["website"]["link"]["url"] == "")
check("the question label is updated", d["47135b5c6df77ad6"]["label"] == "Confirm you are 18 or older")
check("option LABELS change but their KEYS are preserved (answers stay valid)",
      d["47135b5c6df77ad6"]["options"][0]["label"] == "Yes I am"
      and d["47135b5c6df77ad6"]["options"][0]["key"] == "9daa2ad3f6112e54"
      and d["47135b5c6df77ad6"]["options"][1]["label"] == "No")
check("changed-list reports what it touched", set(changed) >= {"privacy_url", "company_name", "thanks_title", "destination_url", "cta_title", "question_label", "question_options"}, changed)

print("-- only the fields you pass change; everything else stays as the template --")
out2, changed2 = lfb.rewrite_form_fields(json.dumps(FORM), {"destination_url": "https://only.example"})
d2 = json.loads(out2)["data"]
check("passing only destination_url leaves privacy/thanks/question exactly as they were",
      d2["customQuestionPrivacy"]["linkList"][0]["linkUrl"] == "https://lovable.dev/privacy"
      and d2["thanksContentContainer"]["title"] == "Thanks for your response!"
      and d2["47135b5c6df77ad6"]["label"] == "Are you 18+? "
      and d2["leadFormCTA"]["buttonsInfo"]["website"]["link"]["url"] == "https://only.example")
check("changed-list is just the destination", changed2 == ["destination_url"], changed2)
check("empty-string edits are ignored (treated as 'leave it')",
      json.loads(lfb.rewrite_form_fields(json.dumps(FORM), {"company_name": ""})[0])["data"]["customQuestionPrivacy"]["companyName"] == "lbvl")
check("unparseable definition → (None, [])", lfb.rewrite_form_fields("{not json", {}) == (None, []))

print("-- build_form: read → create → update → verify → publish --")
calls = []
NEW_DATA_HOLDER = {}
def _read_page(fid, target, source_owner="", shape=""):
    # first read = template; later reads (after create) = the new form carrying the updated data
    if fid == "TEMPLATE":
        return ({"code": "0", "data": {"page_info": {"data": json.dumps(FORM), "business_type": 1, "template_id": "tmpl", "thumbnail_uri": "tos-x/abc"}}}, "target", [])
    return ({"code": "0", "data": {"page_info": {"data": NEW_DATA_HOLDER.get("data", "")}}}, "target", [])
def _post(path, body, target, params=None):
    calls.append((path, body))
    if path == "/v1/create/":
        return {"code": "0", "data": {"page_id": "NEWFORM1"}}
    if path == "/v1/update/":
        NEW_DATA_HOLDER["data"] = body.get("data", "")   # the form now reads back with the edits
        return {"code": "0", "data": {}}
    return {"code": "0", "data": {}}
web = types.ModuleType("app.instant_page_web")
web.read_page = _read_page
web._post = _post
web._ok = lambda b: str((b or {}).get("code", "")) == "0"
web.explain = lambda b, a: "err"
web.thumb_uri = lambda p: "tos-x/abc"
web.POLL_TRIES = 3
web.POLL_S = 0
sys.modules["app.instant_page_web"] = web

res = lfb.build_form("TEMPLATE", "My New Form", "700123", {"destination_url": "https://offer.example/go?x=1", "company_name": "Acme"}, source_owner="700123")
paths = [c[0] for c in calls]
check("it created, then updated, then published — in that order",
      paths == ["/v1/create/", "/v1/update/", f"/v1/publish/NEWFORM1/"], paths)
check("the create call carries the new title + duplicate_id of the template",
      calls[0][1].get("title") == "My New Form" and calls[0][1].get("duplicate_id") == "TEMPLATE")
check("the update call sends the rewritten definition with the new destination",
      "https://offer.example/go?x=1" in calls[1][1].get("data", ""))
check("build_form returns ok with the new form id and what changed",
      res["ok"] is True and res["form_id"] == "NEWFORM1" and "destination_url" in res["changed"])

print("-- a template that can't be read fails cleanly, creates nothing --")
calls.clear()
web.read_page = lambda fid, t, source_owner="", shape="": ({"code": "40002", "data": {}}, "", ["target: 40002"])
res2 = lfb.build_form("BAD", "x", "700123", {})
check("no create call is made when the template read fails", not any(c[0] == "/v1/create/" for c in calls) and res2["ok"] is False)

print("-- route + drawer wiring --")
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
r = read("app/routes/lead_forms.py")
check("a POST /lead-forms/build route calls the builder with the picked account", '@router.post("/lead-forms/build")' in r and "lead_form_builder.build_form(" in r)
check("build guards cookies + workspace scope like clone", "load_cookies()" in r and "sc.allows(from_advertiser_id) and sc.allows(target_advertiser_id)" in r)
check("build re-reads the account so the form shows up, tolerating a failed re-read", "sync_account(db, target)" in r and "db.rollback()" in r.split("def build(")[1].split("\ndef ")[0])
t = read("app/templates/lead_forms.html")
check("the drawer posts to /lead-forms/build with template + target account + name + destination",
      'action="/lead-forms/build"' in t and 'name="template_form_id"' in t and 'name="target_advertiser_id"' in t and 'name="name"' in t and 'name="destination_url"' in t)
check("the template option carries its owner account for the hidden from-id", 'data-owner="{{ f.owner_advertiser_id }}"' in t and 'id="lfFromAdv"' in t)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
