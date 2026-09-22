"""Build a one-button Instant Page on an ad account by driving TikTok's OWN page
builder in a headless browser with the operator's web session.

Why a browser: the Marketing API can only READ pages (/page/get/), and Ads Manager's
own web calls carry per-request anti-bot signatures (msToken, X-Bogus, X-Gnarly — seen
live 17 Sep) that a server can't produce. A real browser running TikTok's JavaScript
produces them itself. The dashboard therefore does what an operator does by hand:
open the Instant Page library, Create → Customize, name the page, set the call-to-action
button, Complete — then checks through the OFFICIAL API that the page now exists.

Honest limits, by design:
  * every step is located by the TEXT TikTok shows the operator (the same labels as the
    builder screen), not by fragile CSS classes; a label TikTok renames makes the step
    fail loudly, with a screenshot, never a silent mis-click;
  * a verification / captcha / login challenge STOPS the build — it is reported, never
    attempted;
  * one browser at a time, and never when the server is already using a lot of memory
    (Render 512 MB); the browser is closed after every page;
  * success is only claimed when /page/get/ lists a page with that name on the account.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import config, spark_web_api

log = logging.getLogger("adops.instant_page_builder")

# Where Chromium lives: the persistent data disk. Render's native runtime doesn't keep the
# build step's ~/.cache, so a browser installed at build time is gone at runtime (seen live
# 17 Sep: "Executable doesn't exist at /opt/render/.cache/ms-playwright/…"). Installing
# into the data disk once — on first use — survives every redeploy. An operator-set
# PLAYWRIGHT_BROWSERS_PATH is respected.
BROWSERS_DIR = config.DATA_DIR / "pw-browsers"
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and (BROWSERS_DIR.exists() or not os.path.isdir(os.path.expanduser("~/.cache/ms-playwright"))):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover — optional dependency
    sync_playwright = None
    PWTimeout = Exception
    PLAYWRIGHT_AVAILABLE = False

_LOCK = threading.Lock()                 # one browser at a time on the 512 MB host
MEMORY_CEILING_MB = 330                  # don't open Chromium when the app already sits above this
STEP_TIMEOUT_MS = 25_000
LIBRARY_WAIT_S = 75                      # Ads Manager's first paint on a small headless box can take a while
LIBRARY_PATH = "/i18n/material/instantPage"          # seen live 17 Sep: ads.tiktok.com/i18n/material/instantPage?aadvid=…
SHOT_DIR = config.DATA_DIR / "page_builds"

# The words TikTok shows on the builder screen (screenshot 17 Sep) — every step keys on these.
T = {
    "create": "Create",
    "customize": "Customize",
    "untitled": "Untitled page",
    "cta_section": "Call to action button",
    "tab_text": "Text",
    "button_text": "Button Text",
    "view_website": "View website",
    "url_placeholder": "Please provide the URL of your website",
    "hand_cursor": "Show hand cursor",
    "bottom": "Set the button to the bottom of the page",
    "complete": "Complete",
    "save": "Save",
}
CHALLENGE_WORDS = ("verify", "verification", "captcha", "security check", "log in", "login", "sign in", "unusual activity")


class BuildError(Exception):
    """A step that couldn't be completed — message names the step; a screenshot exists."""


def available() -> bool:
    return PLAYWRIGHT_AVAILABLE


def memory_ceiling_mb() -> int:
    """How high RSS may climb before we refuse to open Chromium. Scales with the box so the
    upgrade to a bigger Render plan actually lets the browser run: on a ≥1 GB box it's the
    box minus ~500 MB of Chromium headroom; on a small or unknown box it's the conservative
    512-MB-era default (330)."""
    from . import background
    lim = background.mem_limit_mb()
    return max(MEMORY_CEILING_MB, lim - 500) if lim >= 1024 else MEMORY_CEILING_MB


def memory_ok() -> tuple[bool, float]:
    from . import background
    rss = background.rss_mb()
    return (rss == 0.0 or rss < memory_ceiling_mb()), rss


def looks_like_challenge(text: str) -> bool:
    """A page that is asking the human to prove something — never answered by the tool."""
    low = (text or "").lower()
    return any(w in low for w in CHALLENGE_WORDS) and not any(k.lower() in low for k in (T["cta_section"], T["button_text"]))


def _shot(page, tag: str, adv: str) -> str:
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{adv}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{tag}.png"
        page.screenshot(path=str(SHOT_DIR / name), full_page=False)
        return name
    except Exception:  # noqa: BLE001
        return ""


def _hex_ok(v: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", v or ""))


def build(advertiser_id: str, template, base_url: str = "https://ads.tiktok.com", headless: bool = True,
          on_step=None) -> dict:
    """Drive the builder once for one account. Returns
    {ok, steps:[{step, ok, shot}], error, challenge: bool}. Does NOT verify — the caller
    checks /page/get/ (verify_built) because that is the only proof that counts."""
    if not PLAYWRIGHT_AVAILABLE:
        return {"ok": False, "error": "Playwright isn't installed in this deployment (requirements + `playwright install chromium`).", "steps": [], "challenge": False}
    cookies = spark_web_api.load_cookies()
    if not cookies:
        return {"ok": False, "error": "No TikTok web cookies stored — paste them on the TikTok Cookies page.", "steps": [], "challenge": False}
    ok_mem, rss = memory_ok()
    if not ok_mem:
        return {"ok": False, "error": f"Server memory is at {rss:.0f} MB — not opening a browser above {memory_ceiling_mb()} MB. Try again in a few minutes.", "steps": [], "challenge": False, "retry": True}
    steps: list[dict] = []
    adv = str(advertiser_id)

    def step(name, fn):
        if on_step:
            on_step(name)
        try:
            fn()
            steps.append({"step": name, "ok": True})
        except Exception as e:  # noqa: BLE001
            raise BuildError(f"{name}: {str(e).splitlines()[0][:420]}") from e

    with _LOCK:
        for attempt in (1, 2):
            try:
                return _drive(adv, template, cookies, base_url, headless, steps, step)
            except BrowserMissing as e:
                if attempt == 2:
                    return {"ok": False, "error": str(e), "steps": steps, "challenge": False}
                if on_step:
                    on_step("installing Chromium on the server (first run, ~1 min)")
                err = install_browser()
                if err:
                    return {"ok": False, "error": err, "steps": steps, "challenge": False}
                steps.append({"step": "installed Chromium into the data disk", "ok": True})
    return {"ok": False, "error": "browser could not be started", "steps": steps, "challenge": False}


class BrowserMissing(Exception):
    """Playwright is installed but no Chromium build is on this machine."""


def install_browser(timeout: int = 600) -> str:
    """`playwright install chromium` into BROWSERS_DIR (the persistent data disk, so one
    install outlives redeploys — Render drops the build-time cache at runtime). Returns
    '' on success, else the reason."""
    import subprocess
    import sys
    BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_DIR)}
    try:
        proc = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], env=env,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"installing Chromium took longer than {timeout}s — try the build again in a few minutes"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-400:]
        return "Chromium couldn't be installed on this server: " + tail
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)     # the next sync_playwright() looks there
    return ""


def _drive(adv: str, template, cookies: dict, base_url: str, headless: bool, steps: list, step) -> dict:
    """One full run in one browser. Raises BrowserMissing when Chromium isn't installed."""
    if sync_playwright is not None:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=headless, args=["--disable-dev-shm-usage", "--disable-gpu", "--no-first-run"])
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    raise BrowserMissing("Chromium isn't installed on this server yet — " + msg.splitlines()[0][:160]) from e
                if "missing dependencies" in msg.lower() or "Host system" in msg:
                    return {"ok": False, "error": "this server is missing the system libraries Chromium needs: " + msg.splitlines()[0][:200], "steps": steps, "challenge": False}
                raise
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
            ctx.add_cookies([{"name": k, "value": v, "domain": ".tiktok.com", "path": "/"} for k, v in cookies.items()])
            page = ctx.new_page()
            page.set_default_timeout(STEP_TIMEOUT_MS)
            last_shot = ""
            builder = {"page": page}          # the builder screen may open in a second tab (see create_customize)
            try:
                console: list[str] = []
                failed: list[str] = []
                page.on("console", lambda m: console.append(f"{m.type}: {m.text[:100]}") if m.type in ("error", "warning") and len(console) < 6 else None)
                page.on("requestfailed", lambda r: failed.append(f"{r.url[:80]} ({r.failure})") if len(failed) < 4 else None)

                def open_library():
                    resp = page.goto(f"{base_url}{LIBRARY_PATH}?aadvid={adv}", wait_until="domcontentloaded", timeout=60_000)
                    status = resp.status if resp else "?"
                    # Ads Manager is a heavy single-page app: poll up to LIBRARY_WAIT_S for the
                    # Create control (a button OR any element whose text is "Create"), and stop
                    # early with the real reason if TikTok sent us to login / a verification page
                    create = page.get_by_role("button", name=re.compile(r"\bCreate\b", re.I)).or_(
                        page.get_by_text(re.compile(r"^\s*\+?\s*Create\s*$", re.I)))
                    deadline = time.time() + LIBRARY_WAIT_S
                    while True:
                        url = page.url.lower()
                        if "/login" in url or "passport" in url or "sso" in url.split("?")[0]:
                            raise BuildError(f"TikTok sent the browser to its login page ({page.url[:100]}) — the stored cookies don't log this browser in; export ALL cookies for ads.tiktok.com again")
                        body = ""
                        try:
                            body = page.inner_text("body")
                        except Exception:  # noqa: BLE001
                            pass
                        if looks_like_challenge(body):
                            raise BuildError("TikTok is asking for verification / login on this session — not attempted")
                        if create.count() and create.first.is_visible():
                            break
                        if time.time() > deadline:
                            snippet = re.sub(r"\s+", " ", body)[:160]
                            # everything a blank page can tell: HTTP status of the document, its
                            # title, how much HTML arrived, frames, console errors, failed requests
                            try:
                                html_len = len(page.content())
                            except Exception:  # noqa: BLE001
                                html_len = -1
                            detail = (f"HTTP {status}, title “{page.title()[:40]}”, {html_len} bytes of HTML, {len(page.frames)} frame(s)"
                                      + (f", console: {' | '.join(console[:3])}" if console else "")
                                      + (f", failed requests: {' | '.join(failed[:2])}" if failed else ""))
                            raise BuildError(f"no Create button after {LIBRARY_WAIT_S}s at {page.url[:90]} — page says: “{snippet}” [{detail}]")
                        time.sleep(1.0)
                step("open the Instant Page library", open_library)

                def create_customize():
                    # Create may open the builder in a new tab — take whichever appears
                    before = set(ctx.pages)
                    page.get_by_role("button", name=re.compile(r"\bCreate\b", re.I)).or_(
                        page.get_by_text(re.compile(r"^\s*\+?\s*Create\s*$", re.I))).first.click()
                    page.get_by_text(T["customize"], exact=False).first.click()
                    time.sleep(1.5)
                    new = [pg for pg in ctx.pages if pg not in before]
                    target = new[0] if new else page
                    target.set_default_timeout(STEP_TIMEOUT_MS)
                    target.get_by_text(T["cta_section"], exact=False).first.wait_for()
                    builder["page"] = target
                step("Create → Customize", create_customize)

                def name_page():
                    b = builder["page"]
                    # the title row "Untitled page 9/17/26, 00:56 ✎" — click it, then the pencil/editable field
                    title = b.get_by_text(T["untitled"], exact=False).first
                    title.click()
                    box = b.locator("input:focus, [contenteditable=true]:focus").first
                    try:
                        box.wait_for(timeout=4_000)
                    except PWTimeout:
                        # the pencil next to the title (an icon right after the text)
                        title.locator("xpath=following::*[self::svg or self::i or self::button][1]").click()
                        box = b.locator("input:focus, [contenteditable=true]:focus").first
                        box.wait_for(timeout=4_000)
                    b.keyboard.press("Control+A")
                    b.keyboard.type(template.name)
                    b.keyboard.press("Enter")
                    b.get_by_text(template.name, exact=False).first.wait_for()
                step("name the page", name_page)

                def cta_button():
                    b = builder["page"]
                    sec = b.get_by_text(T["cta_section"], exact=False).first
                    sec.click()
                    b.get_by_text(T["tab_text"], exact=True).first.click()
                    field = b.get_by_label(T["button_text"], exact=False).first
                    try:
                        field.wait_for(timeout=4_000)
                    except PWTimeout:
                        # no <label for>: the first input after the "Button Text" heading
                        field = b.get_by_text(T["button_text"], exact=False).first.locator("xpath=following::input[1]")
                    field.fill(template.button_text or "Continue")
                    b.get_by_text(T["view_website"], exact=False).first.click()
                    url_box = b.get_by_placeholder(T["url_placeholder"], exact=False).first
                    url_box.fill(template.url)
                    for label, want in ((T["hand_cursor"], bool(template.hand_cursor)), (T["bottom"], bool(template.bottom_fixed))):
                        row = b.get_by_text(label, exact=False).first
                        cb = row.locator("xpath=preceding::input[@type='checkbox'][1]")
                        try:
                            if cb.is_checked() != want:
                                row.click()
                        except Exception:  # noqa: BLE001 — a checkbox TikTok renders differently: leave its default
                            pass
                step("call-to-action button", cta_button)

                if _hex_ok(template.button_color or ""):
                    def colour():
                        b = builder["page"]
                        # the colour swatch in the style toolbar opens a picker with a hex field
                        b.locator("[class*=color], [class*=colour]").first.click()
                        hexbox = b.locator("input[value^='#'], input[placeholder*='#'], input[maxlength='7']").first
                        hexbox.fill(template.button_color)
                        b.keyboard.press("Enter")
                    try:
                        step("button colour", colour)
                    except BuildError as e:
                        steps.append({"step": "button colour", "ok": False, "note": f"left TikTok's default — {e}"})

                def complete():
                    b = builder["page"]
                    last = _shot(b, "before-complete", adv)
                    steps.append({"step": "screenshot before Complete", "ok": True, "shot": last})
                    b.get_by_role("button", name=re.compile(rf"^\s*{T['complete']}\s*$", re.I)).first.click()
                    # TikTok returns to the library (or shows a confirmation) — wait for the builder screen to go
                    try:
                        b.get_by_text(T["cta_section"], exact=False).first.wait_for(state="hidden", timeout=STEP_TIMEOUT_MS)
                    except PWTimeout:
                        # a confirmation dialog may need one more press
                        b.get_by_role("button", name=re.compile(r"^\s*(Complete|Confirm|OK|Submit)\s*$", re.I)).first.click()
                        b.get_by_text(T["cta_section"], exact=False).first.wait_for(state="hidden", timeout=STEP_TIMEOUT_MS)
                    time.sleep(2)
                    body = b.inner_text("body")
                    if looks_like_challenge(body):
                        raise BuildError("TikTok asked for verification when completing — not attempted")
                step("Complete", complete)
                last_shot = _shot(builder["page"], "after-complete", adv)
                steps.append({"step": "screenshot after Complete", "ok": True, "shot": last_shot})
                return {"ok": True, "steps": steps, "error": "", "challenge": False}
            except BuildError as e:
                challenge = "verification" in str(e).lower()
                shot = _shot(builder.get("page", page), "failed", adv)
                steps.append({"step": "failed", "ok": False, "shot": shot})
                return {"ok": False, "steps": steps, "error": str(e), "challenge": challenge}
            except Exception as e:  # noqa: BLE001
                shot = _shot(builder.get("page", page), "failed", adv)
                steps.append({"step": "failed", "ok": False, "shot": shot})
                return {"ok": False, "steps": steps, "error": f"browser error: {str(e).splitlines()[0][:160]}", "challenge": False}
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass


def verify_built(db, acct, name: str, attempts: int = 4) -> str:
    """The proof: /page/get/ (official API) lists a page with `name` on the account.
    Returns the page_id or ''. Retries briefly — TikTok lists a completed page within
    seconds, not instantly."""
    from .routes import instant_pages as ip
    for i in range(attempts):
        try:
            ip.sync_account(db, acct)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        from . import models
        row = (db.query(models.InstantPage)
               .filter(models.InstantPage.owner_advertiser_id == acct.advertiser_id, models.InstantPage.name == name)
               .order_by(models.InstantPage.id.desc()).first())
        if row:
            return row.page_id
        if i + 1 < attempts:
            time.sleep(3)
    return ""


def build_and_verify(db, acct, template, on_step=None) -> dict:
    """build() then verify_built(): {ok, page_id, error, steps, challenge, retry}."""
    r = build(acct.advertiser_id, template, on_step=on_step)
    if not r.get("ok"):
        return {**r, "page_id": ""}
    pid = verify_built(db, acct, template.name)
    if not pid:
        return {**r, "ok": False, "page_id": "", "error": "the builder finished but /page/get/ doesn't list a page with this name on the account — see the screenshots"}
    return {**r, "page_id": pid}
