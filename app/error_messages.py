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
    # "the TikTok account used in this ad" is the IDENTITY (the profile the ad runs as),
    # not the ad account. Reading it as a broken ad-account connection sends the operator
    # to reconnect something that was never disconnected.
    (re.compile(r"no longer have access to the TikTok account used in this ad|"
                r"select a new identity and creative material", re.I),
     "The ad account can't use that TikTok profile as the ad's identity.",
     "This is about the PROFILE the ad runs as, not the ad account's connection — nothing needs "
     "reconnecting. Either the profile isn't shared to this ad account, or it is shared from a "
     "different Business Center than the one sent with it. Open Assets, check the profile is linked "
     "to this ad account, run the audit, then Retry failed."),
    (re.compile(r"only photo posts can be delivered as carousel", re.I),
     "This spark post is a VIDEO, but the ad was sent as a carousel.",
     "The spark code was marked as a carousel (photo post). The launcher now reads the post's real "
     "type from TikTok, fixes the spark code and retries as a video ad — use Retry failed on this batch."),
    (re.compile(r"(only video|not a video|video posts? can|photo post|carousel)", re.I),
     "The spark post's type (video vs photo carousel) doesn't match the ad format sent.",
     "The launcher now reads the post's real type from TikTok at launch and corrects the spark code — "
     "use Retry failed on this batch."),
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


def fix_for(log) -> dict | None:
    """Where a failed launch can actually be FIXED in the dashboard — {label, href, why} or None.
    Read from the launch log (error_code + message), so the result page can offer a button
    instead of leaving the operator to work out which page handles it."""
    code = str(getattr(log, "error_code", "") or "")
    msg = (getattr(log, "error_message", "") or "")
    low = msg.lower()
    # `error_message` is already the plain-English translation, so match the raw TikTok
    # wording too — otherwise the 40002 friendly text ("…lacks permission…") makes every
    # 40002 look like a broken connection.
    raw = (getattr(log, "error_technical", "") or "").lower()
    adv = getattr(log, "advertiser_id", "") or ""
    spark_id = getattr(log, "spark_code_id", None)
    template_id = getattr(log, "template_id", None)
    if code == "SPARK":
        if "rejected the spark code" in low or "code is incorrect" in low:
            return {"label": "Replace the spark code", "href": f"/spark-codes?edit={spark_id}" if spark_id else "/spark-codes",
                    "why": "Paste a fresh code from the creator, then Retry failed."}
        if "no identity" in low or "could not resolve spark" in low:
            q = f"?account={adv}&fix=1" + (f"&spark={spark_id}" if spark_id else "")
            return {"label": "Connect the creator", "href": "/creators" + q,
                    "why": "Authorise the code on this account or link the creator's profile, then Retry failed."}
        return {"label": "Open Creators", "href": f"/creators?account={adv}", "why": "Check which profiles this account can run ads as."}
    if code == "CONFIG":
        if "caption" in low and "carousel" in low:
            return {"label": "Add the caption", "href": "/creatives?view=carousels", "why": "Open the carousel and type its caption, or add ad text to the preset."}
        if "pixel" in low or "optimization event" in low:
            return {"label": "Open Pixels", "href": "/pixels", "why": "Pick a pixel + event that exists, or fix the preset."}
        if template_id:
            return {"label": "Edit the preset", "href": f"/presets/{template_id}/edit", "why": "The preset's settings need a change."}
        return {"label": "Open Presets", "href": "/presets", "why": "The preset's settings need a change."}
    if code == "ASSET":
        return {"label": "Open Creatives", "href": "/creatives", "why": "The instant page / lead form / creative wasn't found on this account."}
    if re.search(r"tiktok account used in this ad|select a new identity|"
                 r"tiktok profile as the ad's identity", low + " " + raw):
        return {"label": "Open Assets", "href": "/bc-assets?show=all",
                "why": "The profile the ad runs as isn't usable on this ad account — check it is linked there."}
    if code in ("40105", "40113", "40102") or "token" in low or "permission" in low or "reconnect" in low:
        return {"label": "Reconnect the account", "href": f"/accounts?q={adv}", "why": "The connection to this ad account needs renewing."}
    if code == "40100":
        return None      # rate limit — just retry
    if code == "51009" or "budget" in low:
        return {"label": "Edit the preset", "href": f"/presets/{template_id}/edit" if template_id else "/presets", "why": "Raise the budget in the preset."}
    return None
