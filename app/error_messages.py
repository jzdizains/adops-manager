"""Maps raw TikTok error codes to plain-English fixes with an action.

Debugging method that repeatedly works (§6): peel the payload field-by-field
using TikTok's own 40002 messages — TikTok usually names the offending field
and the acceptable values. Fix one, relaunch, read the next.
"""
from __future__ import annotations

import re
import secrets

FRIENDLY: dict[str, tuple[str, str]] = {
    # code: (plain-English meaning, suggested action)
    "0": ("Success.", ""),
    "40001": ("TikTok rejected the request format.",
              "A field in the payload is malformed. Read the technical detail — TikTok names the field."),
    "40002": ("TikTok rejected a field value — or this session lacks permission for this ad account.",
              "Read the technical detail: if it names a field, fix that field and relaunch. "
              "If it mentions permission, the token/cookies are fine but don't cover THIS advertiser — "
              "check the account is under your Business Center."),
    "40100": ("Rate limited by TikTok.", "Wait a minute and retry. Launch fewer accounts at once."),
    "40102": ("Permission denied for this ad account.",
              "The session is valid but has no rights to this advertiser (NOT an expiry). "
              "Confirm the account sits under your Business Center and the app has the needed scopes."),
    "40105": ("Access token invalid or expired.",
              "Reconnect TikTok from the Connect page (Settings → Connect TikTok)."),
    "40113": ("Token lacks a required scope.",
              "Re-authorize the app with all scopes ticked (Ads Management, Reporting, Creative, Pixel, BC)."),
    "50000": ("TikTok internal error.", "Not your fault. Retry once; if it persists, try later."),
    "51009": ("Budget below TikTok's minimum.",
              "Raise the ad group (or CBO campaign) budget — TikTok's minimum is usually $20/day per ad group."),
    "200000": ("TikTok web session expired — genuine cookie expiry.",
               "Paste fresh cookies on the TikTok Cookies page (or push them with the Chrome extension)."),
}

# 40002 messages that actually mean "permission", not "bad field" (§9.6)
_PERMISSION_HINTS = re.compile(r"permission|not authorized|no access|无权限", re.I)

# 40002 messages with a KNOWN cause — matched on TikTok's wording, checked in order
_MESSAGE_HINTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"image size is not supported", re.I),
     "TikTok rejected a carousel slide's pixel size.",
     "Carousel slides must be exactly 720×1280, 640×640 or 1200×628. The launcher now sends a "
     "resized delivery copy of every slide and re-uploads any slide that was uploaded at its "
     "original size earlier — use Retry failed on this batch."),
]


def new_ref() -> str:
    """Short ref id shown on the launch-result page."""
    return secrets.token_hex(3)


def explain(code, raw_message: str = "") -> dict:
    """Return {friendly, action, technical, is_permission} for an error code."""
    key = str(code)
    friendly, action = FRIENDLY.get(
        key, (f"TikTok returned error {key}.",
              "Read the technical detail below — TikTok's message usually names the problem field."))
    is_permission = key in ("40102",) or (key == "40002" and bool(_PERMISSION_HINTS.search(raw_message or "")))
    if is_permission:
        friendly = FRIENDLY["40102"][0]
        action = FRIENDLY["40102"][1]
    elif key == "40002":
        for pat, f, a in _MESSAGE_HINTS:
            if pat.search(raw_message or ""):
                friendly, action = f, a
                break
    return {
        "code": key,
        "friendly": friendly,
        "action": action,
        "technical": (raw_message or "").strip(),
        "is_permission": is_permission,
    }
