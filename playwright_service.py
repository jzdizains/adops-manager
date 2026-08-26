"""Headless-browser actions for the few web-only TikTok tasks that can't even
be replayed as raw web calls. Playwright is OPTIONAL — the app must boot and
run fine without it (it's only needed if you keep these features).

Deploy note: the Render build must run `playwright install chromium`.
"""
from __future__ import annotations

from . import spark_web_api

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover — optional dependency
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False


class PlaywrightUnavailable(Exception):
    pass


def _require():
    if not PLAYWRIGHT_AVAILABLE:
        raise PlaywrightUnavailable(
            "Playwright isn't installed in this deployment. Add `playwright` to "
            "requirements and run `playwright install chromium` in the build.")


def _context_with_cookies(p):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context()
    cookies = spark_web_api.load_cookies()
    if cookies:
        ctx.add_cookies([
            {"name": k, "value": v, "domain": ".tiktok.com", "path": "/"}
            for k, v in cookies.items()
        ])
    return browser, ctx


def screenshot_ads_page(url_path: str, out_file: str) -> str:
    """Open an ads.tiktok.com page with the operator's session and screenshot it
    (used to eyeball web-only states the API can't report)."""
    _require()
    with sync_playwright() as p:
        browser, ctx = _context_with_cookies(p)
        try:
            page = ctx.new_page()
            page.goto(f"https://ads.tiktok.com{url_path}", wait_until="networkidle", timeout=60000)
            page.screenshot(path=out_file, full_page=True)
        finally:
            browser.close()
    return out_file


def run_web_action(url_path: str, actions: list[dict]) -> dict:
    """Generic scripted action runner: [{op: goto|click|fill|wait, selector, value}].
    Kept deliberately small — anything complex belongs in spark_web_api replays."""
    _require()
    results = []
    with sync_playwright() as p:
        browser, ctx = _context_with_cookies(p)
        try:
            page = ctx.new_page()
            page.goto(f"https://ads.tiktok.com{url_path}", wait_until="domcontentloaded", timeout=60000)
            for a in actions:
                op = a.get("op")
                if op == "click":
                    page.click(a["selector"], timeout=15000)
                elif op == "fill":
                    page.fill(a["selector"], a.get("value", ""), timeout=15000)
                elif op == "wait":
                    page.wait_for_selector(a["selector"], timeout=30000)
                results.append({"op": op, "ok": True})
        finally:
            browser.close()
    return {"ok": True, "steps": results}
