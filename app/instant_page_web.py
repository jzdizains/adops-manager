"""Instant Page duplication through the Ads Manager page editor's own web API.

The editor at ads.tiktok.com talks to `/instant_page/api` — an internal API that takes
the normal ads.tiktok.com login cookies (the ones on the TikTok Cookies page) plus the
csrftoken header; no OAuth token, no request signature. The calls below were recorded
from a real "duplicate page" in the editor (operator-supplied, 18 Sep 2026) and are
reproduced as recorded. `account_id` is ALWAYS the TARGET ad account — that is what
puts the copy there.

  1. POST /v1/page_info/{source_page_id}/   {account_id}      → data.page_info
     (data = a JSON *string* holding the page definition, template_id, thumbnail_uri,
      thumbnail, business_type — 6 = a normal page)
  2. POST /v1/create/  {business_type, data, duplicate_id: source, template_id, title,
                        thumbnail_uri, account_id}            → data.page_id
     duplicate_id makes TikTok copy the source server-side and IGNORE edits to `data`,
     so a changed button link needs step 3.
  3. (only when re-pointing the button) poll page_info on the NEW page until code 0
     with page_info.data (up to 20 × 1 s — "record not found" right after create),
     then POST /v1/update/ {data: rewritten, page_id, title, template_id, thumbnail_uri,
     account_id}; then read back and refuse to call it done if the old URL is still in.
  4. POST /v1/publish/{new_page_id}/  {account_id}  — unpublished pages can't run in ads.

Answers are {code, msg, data}; code 0 = success. code 200000, "please log into your
user account", or a non-JSON answer = the cookies are dead → WebAuthError, and the
caller stops the whole run (spark_web_api._web_parse already raises it).

This is an unofficial API and can change without warning; every answer TikTok gives
is written to Diagnostics (kind tiktok-web) so a change shows up as a fact.
"""
from __future__ import annotations

import json
import re
import time

from . import spark_web_api

BASE = "/instant_page/api"
POLL_TRIES = 20
POLL_S = 1.0
_THUMB_RE = re.compile(r"/(tos-[^/]+/[0-9a-f]+)")


class CloneError(Exception):
    """TikTok refused a step (not a session problem — the run continues with the next account)."""


def _headers(target: str) -> dict:
    # the editor sends the library page of the TARGET account as Referer
    return {"Referer": f"{spark_web_api.ADS_BASE}/i18n/material/instantPage?aadvid={target}",
            "Accept": "application/json, text/plain, */*"}


def _post(path: str, body: dict, target: str) -> dict:
    return spark_web_api.web_post(BASE + path, body, headers=_headers(target))


def _ok(body: dict) -> bool:
    return str((body or {}).get("code", "")) == "0"


def _msg(body: dict) -> str:
    return str((body or {}).get("msg") or (body or {}).get("message") or "")[:160]


def page_info(page_id: str, target: str) -> dict:
    return _post(f"/v1/page_info/{page_id}/", {"account_id": str(target)}, target)


def thumb_uri(page: dict) -> str:
    """The `tos-.../<hex>` form TikTok wants back — a full URL is refused ("invalid
    thumbnail url"); when thumbnail_uri is missing it is cut out of `thumbnail`."""
    uri = str(page.get("thumbnail_uri") or "")
    if uri and not uri.startswith("http"):
        return uri
    m = _THUMB_RE.search(str(page.get("thumbnail") or uri or ""))
    return m.group(1) if m else ""


def rewrite_buttons(data: str, url: str, text: str = "") -> str | None:
    """Point EVERY component that carries a string link.url at `url` (and relabel it when
    `text` is given). map.Button is deliberately not trusted — on real pages it named a
    component that didn't exist while the real button kept the old link. Returns the new
    definition string, or None when nothing on the page has a link (the caller must then
    refuse: a copy with the wrong link is worse than no copy)."""
    doc = json.loads(data)
    touched = 0
    comps = doc.get("data") if isinstance(doc, dict) else None
    for comp in (comps or {}).values() if isinstance(comps, dict) else []:
        if isinstance(comp, dict) and isinstance(comp.get("link"), dict) and isinstance(comp["link"].get("url"), str):
            comp["link"]["url"] = url
            if text:
                content = comp.get("content") if isinstance(comp.get("content"), dict) else {}
                comp["content"] = {**content, "text": text}
            touched += 1
    return json.dumps(doc, ensure_ascii=False) if touched else None


def duplicate(source_page_id: str, name: str, target: str, new_url: str = "", new_text: str = "") -> dict:
    """Copy one finished page onto `target`, published. Returns {ok, page_id, steps,
    error}. Raises WebAuthError when the cookies are dead (the caller stops the run);
    any other refusal is reported in `error` with the step it happened at."""
    steps: list[str] = []
    source_page_id, target = str(source_page_id), str(target)
    try:
        info = page_info(source_page_id, target)
        if not _ok(info):
            raise CloneError(f"read source: {info.get('code')} {_msg(info)}")
        page = ((info.get("data") or {}).get("page_info") or {}) if isinstance(info.get("data"), dict) else {}
        if not page.get("data"):
            raise CloneError("source page definition is empty")
        steps.append("read source")
        thumb = thumb_uri(page)
        rewritten = rewrite_buttons(str(page["data"]), new_url, new_text) if new_url else None
        if new_url and not rewritten:
            raise CloneError("no button with a link found in the page — not copying a page whose link can't be changed")

        created = _post("/v1/create/", {
            "business_type": page.get("business_type", 6), "data": page["data"], "duplicate_id": source_page_id,
            "template_id": page.get("template_id"), "title": name, "thumbnail_uri": thumb, "account_id": target,
        }, target)
        if not _ok(created):
            raise CloneError(f"create: {created.get('code')} {_msg(created)}")
        page_id = str(((created.get("data") or {}).get("page_id") if isinstance(created.get("data"), dict) else "") or "")
        if not page_id:
            raise CloneError("create returned no page_id")
        steps.append(f"created {page_id}")

        if rewritten:
            seen = {}
            for _ in range(POLL_TRIES):
                seen = page_info(page_id, target)
                if _ok(seen) and ((seen.get("data") or {}).get("page_info") or {}).get("data"):
                    break
                time.sleep(POLL_S)
            updated = _post("/v1/update/", {
                "data": rewritten, "page_id": page_id, "title": name, "template_id": page.get("template_id"),
                "thumbnail_uri": thumb, "account_id": target,
            }, target)
            if not _ok(updated):
                raise CloneError(f"created {page_id} but update failed: {updated.get('code')} {_msg(updated)}")
            back = page_info(page_id, target)
            pi = ((back.get("data") or {}).get("page_info") or {}) if isinstance(back.get("data"), dict) else {}
            blob = str(pi.get("publish_data") or "") + str(pi.get("data") or "")
            if new_url not in blob:
                raise CloneError(f"created {page_id} and TikTok accepted the update, but the page read back without the new link — not publishing it")
            steps.append("button re-pointed and verified")

        published = _post(f"/v1/publish/{page_id}/", {"account_id": target}, target)
        if not _ok(published):
            raise CloneError(f"created {page_id} but publish failed: {published.get('code')} {_msg(published)}")
        steps.append("published")
        return {"ok": True, "page_id": page_id, "steps": steps, "error": ""}
    except CloneError as e:
        return {"ok": False, "page_id": "", "steps": steps, "error": str(e)}
