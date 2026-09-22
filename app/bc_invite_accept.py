"""Accept a TikTok Business-Center invite by driving the real browser — the same engine
and session the Instant Page builder uses. TikTok gates the accept with per-request
anti-bot signatures (msToken / X-Bogus / X-Gnarly) that a server can't forge; a real
browser running TikTok's own JavaScript produces them itself (verified live 22 Sep).

The invite link opens "Join this Business Center": a name field + a Join button. This
types the name and clicks Join. Two cases are handled as success without a Join click:
  * a RETURNING member (was in the BC before) is dropped straight into the console — no
    Join screen; access is already restored.
The post-Join two-step-verification is NOT a gate here — the Join itself grants access —
so it is never attempted; the CALLER confirms membership through the official API.

Reuses instant_page_builder's browser scaffolding (one browser at a time via the SHARED
lock, the memory ceiling, the on-disk Chromium, screenshots on failure). Never raises."""
from __future__ import annotations

import logging
import re
import time

from . import spark_web_api
from .instant_page_builder import (
    PLAYWRIGHT_AVAILABLE, sync_playwright, BrowserMissing, install_browser,
    memory_ok, memory_ceiling_mb, looks_like_challenge, _shot, _LOCK,
)

log = logging.getLogger("adops.bc_invite_accept")

STEP_TIMEOUT_MS = 25_000
JOIN_WAIT_S = 40
LOGINISH = ("/login", "passport", "/sso")


def accept_invite(invite_url: str, name: str, should_stop=None, on_step=None) -> dict:
    """Drive one accept. Returns {ok, joined, action, detail, error, challenge, retry, shot}.
    `joined` is True when the invite was accepted OR the account already had access; the
    caller still verifies membership through the API, which is the only proof that counts."""
    def say(t: str) -> None:
        if on_step:
            try:
                on_step(t)
            except Exception:  # noqa: BLE001
                pass

    if not PLAYWRIGHT_AVAILABLE or sync_playwright is None:
        return _fail("Playwright isn't installed in this deployment (requirements + `playwright install chromium`).")
    if not (invite_url or "").startswith("https://") or "invite_code=" not in invite_url:
        return _fail("That doesn't look like a TikTok invite link (needs an https …?invite_code=… URL).")
    cookies = spark_web_api.load_cookies()
    if not cookies:
        return _fail("No TikTok web cookies stored — paste them on the TikTok Cookies page (the accept runs as that account).")
    ok_mem, rss = memory_ok()
    if not ok_mem:
        return {**_fail(f"Server memory is at {rss:.0f} MB — not opening a browser above {memory_ceiling_mb()} MB. It retries by itself."), "retry": True}

    with _LOCK:                                  # one browser at a time on the host
        for attempt in (1, 2):
            try:
                return _drive(invite_url, str(name or "").strip(), cookies, should_stop, say)
            except BrowserMissing as e:
                if attempt == 2:
                    return _fail(str(e))
                say("installing Chromium on the server (first run, ~1 min)")
                err = install_browser()
                if err:
                    return _fail(err)
    return _fail("browser could not be started")


def _fail(msg: str, **extra) -> dict:
    return {"ok": False, "joined": False, "action": "", "detail": "", "error": msg,
            "challenge": False, "retry": False, "shot": "", **extra}


def _drive(invite_url: str, name: str, cookies: dict, should_stop, say) -> dict:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True,
                                        args=["--disable-dev-shm-usage", "--disable-gpu", "--no-first-run"])
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise BrowserMissing("Chromium isn't installed on this server yet — " + msg.splitlines()[0][:160]) from e
            if "missing dependencies" in msg.lower() or "Host system" in msg:
                return _fail("this server is missing the system libraries Chromium needs: " + msg.splitlines()[0][:200])
            return _fail("couldn't start the browser: " + msg.splitlines()[0][:200])

        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        # Same as the page builder: the pasted ads.tiktok.com session, scoped to the whole
        # .tiktok.com family so it also authenticates business.tiktok.com (the accept host).
        ctx.add_cookies([{"name": k, "value": v, "domain": ".tiktok.com", "path": "/"} for k, v in cookies.items()])
        page = ctx.new_page()
        page.set_default_timeout(STEP_TIMEOUT_MS)
        shot = ""
        try:
            if should_stop and should_stop():
                return _fail("stopped before starting")
            say("opening the invite link")
            page.goto(invite_url, wait_until="domcontentloaded", timeout=60_000)

            join = page.get_by_role("button", name=re.compile(r"^\s*Join\s*$", re.I))
            name_field = page.get_by_placeholder(re.compile("name", re.I))
            deadline = time.time() + JOIN_WAIT_S
            while True:
                if should_stop and should_stop():
                    return _fail("stopped")
                url = page.url.lower()
                if any(x in url for x in LOGINISH):
                    shot = _shot(page, "accept-login", "invite")
                    return _fail("TikTok sent the browser to its login page — the stored cookies don't log this "
                                 "account in. Re-paste this account's cookies on the TikTok Cookies page.", shot=shot)
                body = ""
                try:
                    body = page.inner_text("body")
                except Exception:  # noqa: BLE001
                    pass
                # A verification/captcha BEFORE we can Join blocks us — reported, never attempted.
                if looks_like_challenge(body):
                    shot = _shot(page, "accept-challenge", "invite")
                    return _fail("TikTok is asking for a security check on this session before the invite can be "
                                 "accepted — do this one by hand, then it works again.", challenge=True, shot=shot)
                if join.count() and join.first.is_visible():
                    break
                # Returning member: no Join screen, dropped into the console → already has access.
                if ("/manage/" in url or "/i18n/" in url) and "invite" not in url:
                    return {"ok": True, "joined": True, "action": "already_member",
                            "detail": "No Join screen — the account already has access to this Business Center.",
                            "error": "", "challenge": False, "retry": False, "shot": ""}
                if time.time() > deadline:
                    # No Join button and not obviously in the console: safest to report, not guess.
                    shot = _shot(page, "accept-nojoin", "invite")
                    return {"ok": True, "joined": False, "action": "no_join",
                            "detail": "No Join screen appeared within the wait — the invite may already be "
                                      "accepted or expired. The caller verifies via the API.",
                            "error": "", "challenge": False, "retry": True, "shot": shot}
                page.wait_for_timeout(800)

            # Join screen is up. Fill the name if the field is there (a returning name may be preset).
            say("filling the name and joining")
            try:
                if name and name_field.count() and name_field.first.is_visible():
                    name_field.first.fill(name)
            except Exception:  # noqa: BLE001
                pass
            join.first.click()
            # Give TikTok a moment to register the Join. We do NOT touch the 2FA that may
            # follow — the Join already granted access; the caller confirms via the API.
            page.wait_for_timeout(2500)
            return {"ok": True, "joined": True, "action": "joined",
                    "detail": "Clicked Join. Any 2-step page after this is a separate login step and is left "
                              "alone; membership is confirmed through the API.",
                    "error": "", "challenge": False, "retry": False, "shot": ""}
        except Exception as e:  # noqa: BLE001
            try:
                shot = _shot(page, "accept-error", "invite")
            except Exception:  # noqa: BLE001
                shot = ""
            return _fail("accept failed: " + str(e).splitlines()[0][:300], shot=shot)
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:  # noqa: BLE001
                pass
