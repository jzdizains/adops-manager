"""TikTok's Lead Generation Terms, per ad account, through the official API (v155.15).

What was found (23–24 Sep 2026): /ad/create/ (and /smart_plus/ad/create/) refuse an Instant Form
ad with "Lead Generation agreement has not be signed yet" on every account — the same login, form
and post go through when the campaign is built by hand in Ads Manager. Ads Manager signs its own
agreements on the web (agreement/general_sign) at the first hand-built lead ad; copying those
changed nothing for the API, because the Marketing API keeps a SEPARATE record of confirmed Terms
per ad account: /term/check/, /term/get/ and /term/confirm/ (Account management › Terms).

The exact `term_type` name for the lead-gen Terms isn't documented anywhere reachable without a
developer login, so it is discovered once: TikTok's refusal of a wrong value lists the accepted
ones; the one about lead generation is picked, remembered (Setting `lead_term_type`) and written to
Diagnostics. Nothing here confirms anything by itself — only the operator's button does.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from . import tiktok_api

log = logging.getLogger("adops.lead_terms")

TERM_TYPE_KEY = "lead_term_type"
# TikTok's own list (24 Sep 2026, /term/check/ refusing a wrong value): "correct is InstantPage,
# LeadAds, Pixel, ReachFrequency". LeadAds is tried first; the rest of the discovery stays for the
# day TikTok renames it.
CANDIDATES = ("LeadAds", "LEAD_GEN_TERMS", "LEAD_GENERATION_TERMS")
OK_TTL = 24 * 3600
NO_TTL = 300
_MAX = 2000
_cache: dict = {}                 # advertiser_id → (expires, True | False)
_lock = threading.Lock()
_type_lock = threading.Lock()
_quoted = re.compile(r"['\"]([A-Za-z][A-Za-z0-9_]{2,60})['\"]")
_listed = re.compile(r"(?:correct is|must be one of|allowed values? (?:are|is))\s*:?\s*\[?\s*([A-Za-z0-9_ ,'\"]+)", re.I)


class Unknown(Exception):
    """TikTok couldn't be asked (no token, network, unexpected answer) — never a guess."""


def pick_type(candidates, message: str) -> str:
    """The lead-gen term type out of TikTok's "must be one of …" refusal. Pure."""
    msg = message or ""
    names = list(_quoted.findall(msg))
    for m in _listed.finditer(msg):          # every "correct is … / one of …" list in the message
        names += [x.strip(" '\"") for x in m.group(1).split(",")]
    for n in names:
        if n and n not in candidates and "lead" in n.lower():
            return n
    return ""


def confirmed_of(data: dict):
    """True/False out of /term/check/'s answer, whatever it calls the flag; None when unreadable. Pure."""
    d = data or {}
    for k in ("is_confirmed", "confirmed", "is_signed", "signed", "is_agreed", "agreed", "has_confirmed", "status"):
        if k in d:
            v = d[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                return v.upper() in ("TRUE", "1", "CONFIRMED", "SIGNED", "AGREED", "YES")
    return None


def _note(where: str, code, message: str, ctx: dict | None = None) -> None:
    try:
        from . import diag
        diag.record("tiktok", where, code, message, ctx or {})
    except Exception:  # noqa: BLE001
        pass


def term_type(db, token: str, adv: str) -> str:
    """The lead-gen `term_type` TikTok accepts — remembered after the first discovery."""
    from . import queries
    known = queries.get_setting(db, TERM_TYPE_KEY, "")
    if known:
        return known
    with _type_lock:
        known = queries.get_setting(db, TERM_TYPE_KEY, "")
        if known:
            return known
        last = ""
        for cand in CANDIDATES:
            try:
                tiktok_api.term_check(token, adv, cand)
                found = cand                         # accepted as-is
            except tiktok_api.TikTokError as e:
                last = f"{e.code} {e.message}"
                found = pick_type(CANDIDATES, e.message or "")
                if not found:
                    continue
            queries.set_setting(db, TERM_TYPE_KEY, found)
            _note("/term/check/", "term_type", f"lead-gen Terms type discovered: {found}", {"tried": cand, "answer": last[:300]})
            log.info("lead-gen term_type discovered: %s", found)
            return found
        _note("/term/check/", "term_type", f"couldn't discover the lead-gen term_type — last answer: {last[:300]}", {"tried": list(CANDIDATES)})
        raise Unknown(f"TikTok didn't accept any known term_type ({last[:160]})")


def _token(db, adv: str) -> str:
    from . import models
    a = db.query(models.AdAccount).filter_by(advertiser_id=str(adv)).first()
    if a is None or not a.access_token:
        raise Unknown("that ad account isn't connected")
    return a.access_token


def cached(adv: str):
    with _lock:
        hit = _cache.get(str(adv))
    return hit[1] if hit and hit[0] > time.time() else None


def _remember(adv: str, ok: bool) -> None:
    with _lock:
        if len(_cache) >= _MAX:
            _cache.clear()
        _cache[str(adv)] = (time.time() + (OK_TTL if ok else NO_TTL), ok)


def status(db, adv: str, fresh: bool = False):
    """True = confirmed, False = not yet, None = couldn't tell (the launch then simply tries)."""
    adv = str(adv)
    if not fresh:
        c = cached(adv)
        if c is not None:
            return c
    try:
        tok = _token(db, adv)
        d = tiktok_api.term_check(tok, adv, term_type(db, tok, adv))
    except (Unknown, tiktok_api.TikTokError) as e:
        log.info("lead terms status unknown for %s: %s", adv, e)
        return None
    ok = confirmed_of(d)
    if ok is None:
        _note("/term/check/", "shape", f"unexpected /term/check/ answer: {json.dumps(d)[:300]}", {"advertiser_id": adv})
        return None
    _remember(adv, ok)
    return ok


def text(db, adv: str) -> str:
    """The Terms' text (for the confirm dialog); "" when TikTok won't give it."""
    try:
        tok = _token(db, adv)
        d = tiktok_api.term_get(tok, adv, term_type(db, tok, adv))
    except (Unknown, tiktok_api.TikTokError):
        return ""
    for k in ("term_content", "content", "text", "terms", "term"):
        if isinstance(d.get(k), str) and d[k].strip():
            return d[k]
    return ""


def accept(db, adv: str) -> tuple[bool, str]:
    """Confirm the lead-gen Terms for this ad account through the official API, then read it
    back. (ok, message). Only the operator's button calls this."""
    adv = str(adv)
    try:
        tok = _token(db, adv)
        tt = term_type(db, tok, adv)
        if confirmed_of(tiktok_api.term_check(tok, adv, tt)) is True:
            _remember(adv, True)
            return True, "already confirmed"
        tiktok_api.term_confirm(tok, adv, tt)
        ok = confirmed_of(tiktok_api.term_check(tok, adv, tt))
    except Unknown as e:
        return False, str(e)
    except tiktok_api.TikTokError as e:
        return False, f"TikTok refused: {e.message} (code {e.code})"
    if ok is True:
        _remember(adv, True)
        return True, "confirmed"
    if ok is None:
        _remember(adv, True)          # confirm was accepted; the read-back shape is just unknown
        return True, "confirmed (TikTok accepted the confirmation)"
    return False, "TikTok accepted the confirmation but still reports the Terms as not confirmed"
