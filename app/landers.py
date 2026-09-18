"""Landers (v124) — pages the dashboard builds for the operator's OWN domains.

A lander is a template + a row of settings (models.Lander). `build_html` renders the
template with the settings baked in as window.LANDER_CFG, the shared runtime
(static/lander.js) and the click script (static/pass-source.js) inlined, so the export is
ONE self-contained index.html that goes on Hostinger / Vercel / anywhere. At open, the page
re-reads the live settings from /t/l/<slug>.json (public, 60 s cache), so the next URL,
the routing rules and the escape method change without re-uploading.

Honest by design: no cloaking, no fake states, no decoys. Detection only adapts the page.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import jinja2

from . import models

ROOT = Path(__file__).resolve().parent
TPL_DIR = ROOT / "lander_templates"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
URL_RE = re.compile(r"^https?://[^\s\"'<>]+$", re.I)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# escape methods the runtime knows (see static/lander.js L.escapeUrl) — the escape test's lab
ESCAPE_ANDROID = [("chrome-intent", "Chrome intent (package + https fallback)"), ("intent-default", "Package-less intent → default browser"),
                  ("intent-browsable", "Intent with VIEW + BROWSABLE"), ("intent-open", "Chrome intent via window.open"),
                  ("chrome-scheme", "googlechrome:// scheme"), ("direct", "Don't try — continue in-app")]
ESCAPE_IOS = [("x-safari", "x-safari- scheme (navigation)"), ("x-safari-open", "x-safari- via window.open"),
              ("shortcuts", "Shortcuts x-callback fallback"), ("chrome-ios", "googlechromes:// (Chrome for iOS)"),
              ("firefox-ios", "firefox://open-url"), ("direct", "Don't try — continue in-app")]
RULE_OS = ("", "ios", "android", "other")
RULE_INAPP = ("", "yes", "no")
AGE_BRACKETS = ("u18", "18_24", "25_34", "35_49", "50p")
PIXEL_EVENTS = ("", "ClickButton", "ViewContent", "Contact", "SubmitForm", "Subscribe", "CompleteRegistration", "Download")

TEMPLATES = {
    "prelander": {
        "label": "Open-in-browser prelander",
        "blurb": "One honest step between the ad and your lander: in the TikTok app it offers to open your page in the phone's real browser "
                 "(the escape test's methods); in a real browser it continues straight away. Every tap is a real tap.",
        "file": "prelander.html",
        "defaults": {
            "brand": "", "headline": "Open in your browser", "text": "Tap Continue to open this page in your browser for the full experience.",
            "button": "Continue", "note": "You'll be taken to Safari or Chrome. Nothing is installed.", "inbrowser_text": "Tap Continue to carry on.",
            "theme": "dark", "accent": "#6d4cff", "next": "", "rules": [], "escape": {"android": "chrome-intent", "ios": "x-safari"},
            "pixel": "", "pixel_event": "ClickButton", "live_budget_ms": 1500,
        },
    },
}


def valid_slug(s: str) -> bool:
    return bool(SLUG_RE.match(s or ""))


def parse_rules(raw) -> tuple[list[dict], list[str]]:
    """The editor's rules (JSON list or already a list) → clean rules + problems."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            return [], ["Rules are not valid JSON."]
    if not isinstance(raw, list):
        return [], ["Rules must be a list."]
    out, problems = [], []
    for i, r in enumerate(raw[:50]):
        if not isinstance(r, dict):
            continue
        when = r.get("when") if isinstance(r.get("when"), dict) else {}
        to = str(r.get("to") or "").strip()
        if not URL_RE.match(to):
            problems.append(f"Rule {i + 1}: destination must be a full http(s) URL.")
            continue
        clean = {"name": str(r.get("name") or "")[:40], "to": to, "when": {}}
        os_ = str(when.get("os") or "").lower().strip()
        if os_ and os_ not in RULE_OS[1:]:
            problems.append(f"Rule {i + 1}: os must be ios, android or other.")
        elif os_:
            clean["when"]["os"] = os_
        inapp = str(when.get("inapp") or "").lower().strip()
        if inapp in ("yes", "no"):
            clean["when"]["inapp"] = inapp
        age = [a.strip() for a in str(when.get("age") or "").split(",") if a.strip()]
        bad = [a for a in age if a not in AGE_BRACKETS]
        if bad:
            problems.append(f"Rule {i + 1}: unknown age bracket {', '.join(bad)} (use {', '.join(AGE_BRACKETS)}).")
        elif age:
            clean["when"]["age"] = ",".join(age)
        country = [c.strip().upper() for c in str(when.get("country") or "").split(",") if c.strip()]
        if any(not re.match(r"^[A-Z]{2}$", c) for c in country):
            problems.append(f"Rule {i + 1}: countries are two-letter codes (US, GB, DE).")
        elif country:
            clean["when"]["country"] = ",".join(country)
        hours = str(when.get("hours") or "").strip()
        if hours and not re.match(r"^\d{1,2}\s*-\s*\d{1,2}$", hours):
            problems.append(f"Rule {i + 1}: hours look like 9-17 (visitor's local time).")
        elif hours:
            clean["when"]["hours"] = hours
        out.append(clean)
    return out, problems


def clean_config(template: str, raw: dict) -> tuple[dict, list[str]]:
    """Form/JSON → the config stored on the row, with the template's defaults filled in."""
    t = TEMPLATES.get(template) or TEMPLATES["prelander"]
    cfg = dict(t["defaults"])
    problems: list[str] = []
    for k in ("brand", "headline", "text", "button", "note", "inbrowser_text"):
        if k in raw:
            cfg[k] = str(raw.get(k) or "")[:300]
    if str(raw.get("theme") or "") in ("dark", "light"):
        cfg["theme"] = str(raw["theme"])
    accent = str(raw.get("accent") or "").strip()
    if accent:
        if COLOR_RE.match(accent):
            cfg["accent"] = accent
        else:
            problems.append("Accent colour must look like #6d4cff.")
    nxt = str(raw.get("next") or "").strip()
    if nxt and not URL_RE.match(nxt):
        problems.append("Next page must be a full http(s) URL.")
    cfg["next"] = nxt if URL_RE.match(nxt) else ""
    rules, rp = parse_rules(raw.get("rules", cfg.get("rules", [])))
    cfg["rules"] = rules
    problems += rp
    esc = raw.get("escape") if isinstance(raw.get("escape"), dict) else {"android": raw.get("escape_android"), "ios": raw.get("escape_ios")}
    cfg["escape"] = {
        "android": esc.get("android") if esc.get("android") in dict(ESCAPE_ANDROID) else "chrome-intent",
        "ios": esc.get("ios") if esc.get("ios") in dict(ESCAPE_IOS) else "x-safari",
    }
    pixel = re.sub(r"[^A-Za-z0-9]", "", str(raw.get("pixel") or ""))[:40]
    cfg["pixel"] = pixel
    ev = str(raw.get("pixel_event") or "")
    cfg["pixel_event"] = ev if ev in PIXEL_EVENTS else "ClickButton"
    if not cfg["next"] and not cfg["rules"]:
        problems.append("Set the next page (where Continue goes) or at least one rule.")
    return cfg, problems


def config_of(row: models.Lander) -> dict:
    t = TEMPLATES.get(row.template) or TEMPLATES["prelander"]
    cfg = dict(t["defaults"])
    try:
        cfg.update(json.loads(row.config or "{}"))
    except (ValueError, TypeError):
        pass
    return cfg


def public_config(row: models.Lander) -> dict:
    """What /t/l/<slug>.json answers: only what the page needs to route — never owner ids."""
    cfg = config_of(row)
    return {"slug": row.slug, "template": row.template, "next": cfg.get("next", ""), "rules": cfg.get("rules", []),
            "escape": cfg.get("escape", {}), "pixel_event": cfg.get("pixel_event", ""), "enabled": bool(row.enabled)}


def _json_for_script(obj) -> str:
    """JSON that is safe inside a <script>: no '</' sequence can close the tag."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL_DIR)), autoescape=True, undefined=jinja2.StrictUndefined)


def build_html(row: models.Lander, track_host: str, extra_params: str = "") -> str:
    """The self-contained page: template + baked config + inlined runtime and click script."""
    t = TEMPLATES.get(row.template) or TEMPLATES["prelander"]
    cfg = config_of(row)
    baked = {**cfg, "slug": row.slug, "track_host": track_host.rstrip("/")}
    runtime = (ROOT / "static" / "lander.js").read_text(encoding="utf-8")
    pass_js = ((ROOT / "static" / "pass-source.js").read_text(encoding="utf-8")
               .replace("__ADOPS_TRACK_HOST__", track_host.rstrip("/")).replace("__ADOPS_EXTRA_PARAMS__", extra_params or ""))
    tpl = _env.get_template(t["file"])
    return tpl.render(cfg=cfg, cfg_json=_json_for_script(baked), runtime_js=runtime.replace("</script", "<\\/script"),
                      pass_js=pass_js.replace("</script", "<\\/script"), slug=row.slug, name=row.name or row.slug)


def package(row: models.Lander, track_host: str, extra_params: str = "") -> bytes:
    """A zip with index.html (and a robots.txt that keeps crawlers off the page)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", build_html(row, track_host, extra_params))
        z.writestr("robots.txt", "User-agent: *\nDisallow: /\n")
    return buf.getvalue()
