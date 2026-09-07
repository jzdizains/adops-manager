"""Tiny User-Agent reader for the access log — browser, OS, device class.
Good enough to tell 'Chrome on Windows' from 'Safari on iPhone'; no library."""
from __future__ import annotations

import re


def parse(ua: str) -> dict:
    ua = ua or ""
    u = ua.lower()
    if not ua:
        return {"browser": "", "os": "", "device": "", "summary": ""}
    # device / OS
    if "iphone" in u:
        os_, device = "iOS", "phone"
    elif "ipad" in u:
        os_, device = "iPadOS", "tablet"
    elif "android" in u:
        os_, device = "Android", ("tablet" if "mobile" not in u else "phone")
    elif "windows" in u:
        os_, device = "Windows", "desktop"
    elif "mac os x" in u or "macintosh" in u:
        os_, device = "macOS", "desktop"
    elif "cros" in u:
        os_, device = "ChromeOS", "desktop"
    elif "linux" in u:
        os_, device = "Linux", "desktop"
    else:
        os_, device = "", ""
    # browser (order matters: Edge/Opera/Chrome embed each other's tokens)
    if "edg/" in u or "edge/" in u:
        browser = "Edge"
    elif "opr/" in u or "opera" in u:
        browser = "Opera"
    elif "firefox/" in u or "fxios/" in u:
        browser = "Firefox"
    elif "crios/" in u:
        browser = "Chrome"
    elif "chrome/" in u or "chromium/" in u:
        browser = "Chrome"
    elif "safari/" in u and "version/" in u:
        browser = "Safari"
    elif re.search(r"\b(curl|wget|python-requests|python-httpx|httpx|go-http-client|okhttp|java/|libwww|scrapy|bot|spider|crawler|masscan|nmap|zgrab)\b", u):
        browser = "script / bot"
        device = device or "bot"
    else:
        browser = ""
    parts = [p for p in (browser, os_) if p]
    summary = " on ".join(parts) if len(parts) == 2 else (parts[0] if parts else ua[:60])
    return {"browser": browser, "os": os_, "device": device, "summary": summary}
