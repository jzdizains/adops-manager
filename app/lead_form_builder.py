"""Build a new TikTok instant (lead) form on ONE account from an existing form as
template, overriding the key fields — through the page-editor web session, the same
proven create → update → publish flow the clone uses (no headless browser).

Why a template + overrides, not a from-scratch build: a TikTok instant form is a tree
of ~90 "brick" components (LpPage/LpSubpage/LpAgreement/LpFormCTA/…). Re-creating that
tree by hand is brittle; duplicating a known-good form and rewriting only the handful of
fields the operator sets is robust. The field→brick mapping below was read from a real
form (7688359276615762197, 22 Sep 2026), not guessed:

  form name          → the /v1/create/ + /v1/update/ `title` param (not in the tree)
  question label     → every Lp*Field brick's `label`
  question options   → that brick's `options[].label` (keys kept, so answers stay valid)
  privacy URL + name → every LpAgreement brick's linkList[].linkUrl / linkText / companyName
  thank-you screen   → the LEAD LpThanksPage brick's `title` / `description`
  destination link   → the LEAD LpFormCTA brick's buttonsInfo.website.link.url / .title
                       (where a submitted lead is sent — the offer link)

The non-lead (disqualified) thank-you page and CTA are deliberately left untouched.
"""
from __future__ import annotations

import json
import time

QUESTION_FIELD_NAMES = (
    "LpMultipleChoiceField", "LpSingleChoiceField", "LpChoiceField",
    "LpTextField", "LpEmailField", "LpPhoneField", "LpDropdownField",
)


def _is_nonlead(brick_index: str) -> bool:
    return str(brick_index or "").startswith("nonLead")


def rewrite_form_fields(data_str: str, edits: dict) -> tuple[str | None, list[str]]:
    """Return (new definition JSON string, list of fields changed), or (None, []) when
    the definition can't be parsed. `edits` may carry any of: privacy_url, company_name,
    thanks_title, thanks_description, destination_url, cta_title, question_label,
    question_options (list[str]). Only keys that are present AND not None are applied;
    everything else in the form is left exactly as the template had it."""
    try:
        doc = json.loads(data_str) if isinstance(data_str, str) else data_str
    except (ValueError, TypeError):
        return None, []
    comps = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(comps, dict):
        return None, []

    def g(key):
        v = edits.get(key)
        return v if v not in (None, "") else None

    privacy_url, company = g("privacy_url"), g("company_name")
    thanks_title, thanks_desc = g("thanks_title"), g("thanks_description")
    dest_url, cta_title = g("destination_url"), g("cta_title")
    q_label = g("question_label")
    q_options = edits.get("question_options")
    if not isinstance(q_options, list) or not q_options:
        q_options = None

    changed: set[str] = set()
    for brick_index, c in comps.items():
        if not isinstance(c, dict):
            continue
        name = c.get("name")

        if name == "LpAgreement" and (privacy_url or company):
            for ln in (c.get("linkList") or []):
                if not isinstance(ln, dict):
                    continue
                if privacy_url and isinstance(ln.get("linkUrl"), str):
                    ln["linkUrl"] = privacy_url
                    changed.add("privacy_url")
                if company:
                    ln["linkText"] = f"View {company}’s privacy policy."
            if company and "companyName" in c:
                c["companyName"] = company
                changed.add("company_name")

        elif name == "LpThanksPage" and not _is_nonlead(c.get("brickIndex")):
            if thanks_title:
                c["title"] = thanks_title
                changed.add("thanks_title")
            if thanks_desc:
                c["description"] = thanks_desc
                changed.add("thanks_description")

        elif name == "LpFormCTA" and not _is_nonlead(c.get("brickIndex")) and (dest_url or cta_title):
            bi = c.get("buttonsInfo") if isinstance(c.get("buttonsInfo"), dict) else {}
            web = bi.get("website") if isinstance(bi.get("website"), dict) else None
            if isinstance(web, dict):
                if cta_title:
                    web["title"] = cta_title
                    changed.add("cta_title")
                link = web.get("link") if isinstance(web.get("link"), dict) else None
                if isinstance(link, dict) and dest_url:
                    link["url"] = dest_url
                    changed.add("destination_url")

        elif name in QUESTION_FIELD_NAMES:
            if q_label and "label" in c:
                c["label"] = q_label
                changed.add("question_label")
            if q_options and isinstance(c.get("options"), list):
                for i, opt in enumerate(c["options"]):
                    if i < len(q_options) and isinstance(opt, dict):
                        opt["label"] = q_options[i]
                changed.add("question_options")

    return json.dumps(doc, ensure_ascii=False), sorted(changed)


def extract_form_fields(data_str, title: str = "") -> dict:
    """The reverse of rewrite_form_fields: read a live form's definition and pull out the
    handful of human-facing fields, so the same TikTok-style phone preview the builder uses
    can render an EXISTING form. Returns {title, company_name, privacy_url, question_label,
    question_options, cta_title, destination_url, thanks_title, thanks_description}. Missing
    pieces come back empty — the preview falls back to its own placeholders."""
    out = {"title": title or "", "company_name": "", "privacy_url": "", "question_label": "",
           "question_options": [], "cta_title": "", "destination_url": "",
           "thanks_title": "", "thanks_description": ""}
    try:
        doc = json.loads(data_str) if isinstance(data_str, str) else data_str
    except (ValueError, TypeError):
        return out
    comps = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(comps, dict):
        return out
    for c in comps.values():
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if name == "LpAgreement":
            if not out["company_name"] and c.get("companyName"):
                out["company_name"] = str(c.get("companyName") or "")
            for ln in (c.get("linkList") or []):
                if isinstance(ln, dict) and not out["privacy_url"] and isinstance(ln.get("linkUrl"), str):
                    out["privacy_url"] = ln["linkUrl"]
        elif name == "LpThanksPage" and not _is_nonlead(c.get("brickIndex")):
            if not out["thanks_title"] and c.get("title"):
                out["thanks_title"] = str(c.get("title") or "")
            if not out["thanks_description"] and c.get("description"):
                out["thanks_description"] = str(c.get("description") or "")
        elif name == "LpFormCTA" and not _is_nonlead(c.get("brickIndex")):
            bi = c.get("buttonsInfo") if isinstance(c.get("buttonsInfo"), dict) else {}
            web = bi.get("website") if isinstance(bi.get("website"), dict) else None
            if isinstance(web, dict):
                if not out["cta_title"] and web.get("title"):
                    out["cta_title"] = str(web.get("title") or "")
                link = web.get("link") if isinstance(web.get("link"), dict) else None
                if isinstance(link, dict) and not out["destination_url"] and link.get("url"):
                    out["destination_url"] = str(link.get("url") or "")
        elif name in QUESTION_FIELD_NAMES:
            if not out["question_label"] and c.get("label"):
                out["question_label"] = str(c.get("label") or "")
            if not out["question_options"] and isinstance(c.get("options"), list):
                out["question_options"] = [str(o.get("label") or "") for o in c["options"]
                                           if isinstance(o, dict) and o.get("label")]
    return out


def build_form(template_form_id: str, name: str, target: str, edits: dict,
               source_owner: str = "") -> dict:
    """Create a new instant form named `name` on account `target`, copied from
    `template_form_id` with `edits` applied, then verified and published. Returns
    {ok, form_id, steps, changed, error}. Reuses the proven page-editor web flow;
    raises nothing the caller isn't already handling for the clone path."""
    from . import instant_page_web as web
    steps: list[str] = []
    template_form_id, target, source_owner = str(template_form_id), str(target), str(source_owner or "")

    info, shape, probes = web.read_page(template_form_id, target, source_owner)
    if not web._ok(info):
        return {"ok": False, "form_id": "", "steps": steps, "changed": [],
                "error": "read template: " + web.explain(info, target) + (" — tried " + "; ".join(probes) if probes else "")}
    page = ((info.get("data") or {}).get("page_info") or {}) if isinstance(info.get("data"), dict) else {}
    if not page.get("data"):
        return {"ok": False, "form_id": "", "steps": steps, "changed": [], "error": "template definition is empty"}
    steps.append("read template")

    new_data, changed = rewrite_form_fields(str(page["data"]), edits)
    if new_data is None:
        return {"ok": False, "form_id": "", "steps": steps, "changed": [], "error": "couldn't parse the template's definition"}

    thumb = web.thumb_uri(page)
    created = web._post("/v1/create/", {
        "business_type": page.get("business_type", 1), "data": page["data"], "duplicate_id": template_form_id,
        "template_id": page.get("template_id"), "title": name, "thumbnail_uri": thumb, "account_id": target,
    }, target)
    if not web._ok(created):
        return {"ok": False, "form_id": "", "steps": steps, "changed": changed, "error": "create: " + web.explain(created, target)}
    form_id = str(((created.get("data") or {}).get("page_id") if isinstance(created.get("data"), dict) else "") or "")
    if not form_id:
        return {"ok": False, "form_id": "", "steps": steps, "changed": changed, "error": "create returned no form id"}
    steps.append(f"created {form_id}")

    # duplicate_id copies the source verbatim and ignores the data we sent, so the edits
    # go in as a follow-up update — after the new form is readable.
    if changed:
        for _ in range(web.POLL_TRIES):
            seen, _s, _p = web.read_page(form_id, target, source_owner, shape=shape)
            if web._ok(seen) and ((seen.get("data") or {}).get("page_info") or {}).get("data"):
                break
            time.sleep(web.POLL_S)
        updated = web._post("/v1/update/", {
            "data": new_data, "page_id": form_id, "title": name, "template_id": page.get("template_id"),
            "thumbnail_uri": thumb, "account_id": target,
        }, target)
        if not web._ok(updated):
            return {"ok": False, "form_id": form_id, "steps": steps, "changed": changed,
                    "error": f"created {form_id} but applying the fields failed: " + web.explain(updated, target)}
        # verify at least the destination link actually landed before publishing
        if edits.get("destination_url"):
            back, _s, _p = web.read_page(form_id, target, source_owner, shape=shape)
            pi = ((back.get("data") or {}).get("page_info") or {}) if isinstance(back.get("data"), dict) else {}
            blob = str(pi.get("publish_data") or "") + str(pi.get("data") or "")
            if edits["destination_url"] not in blob:
                return {"ok": False, "form_id": form_id, "steps": steps, "changed": changed,
                        "error": f"created {form_id} and TikTok accepted the update, but the destination link didn't read back — not publishing it"}
        steps.append("fields applied and verified")

    published = web._post(f"/v1/publish/{form_id}/", {"account_id": target}, target)
    if not web._ok(published):
        return {"ok": False, "form_id": form_id, "steps": steps, "changed": changed,
                "error": f"created {form_id} but publish failed: " + web.explain(published, target)}
    steps.append("published")
    return {"ok": True, "form_id": form_id, "steps": steps, "changed": changed, "error": ""}
