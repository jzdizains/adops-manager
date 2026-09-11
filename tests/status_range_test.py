"""Campaigns view — the date range filter.

The custom range used to do nothing: choosing "Custom" revealed the two date boxes,
but no control ever submitted the form, so the table kept showing the old range.
Covers the server side (bounds, back-to-front dates, junk dates) and the page itself.
"""
import os, sys, tempfile, threading, time
os.environ.setdefault("ADOPS_DISABLE_BG", "1")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="adops-range-")
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qs
from fastapi.testclient import TestClient
from app.main import app
from app import config, models, timeutil
from app.database import SessionLocal

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

# ---- 1. range_bounds -------------------------------------------------------------
s, e = timeutil.range_bounds("custom", "2026-09-01", "2026-09-11")
check("custom range covers whole local days, end exclusive (11 days)", (e - s) == timedelta(days=11), f"{s} → {e}")
s2, e2 = timeutil.range_bounds("custom", "2026-09-11", "2026-09-01")
check("dates picked back to front give the same range, not an empty one", (s2, e2) == (s, e), f"{s2} → {e2}")
today_s, today_e = timeutil.range_bounds("today")
check("custom with only one date falls back to today", timeutil.range_bounds("custom", "2026-09-01", None) == (today_s, today_e))
check("a junk date falls back to today instead of raising", timeutil.range_bounds("custom", "not-a-date", "2026-09-11") == (today_s, today_e))

# ---- 2. the page ------------------------------------------------------------------
db = SessionLocal()
db.add(models.AdAccount(advertiser_id="A1", advertiser_name="blue bat", access_token="t", enabled=True, status="ENABLE"))
db.commit(); db.close()
c = TestClient(app); c.post("/login", data={"email": config.OWNER_EMAIL, "password": config.APP_PASSWORD})
h = c.get("/status?range=custom&start=2026-09-01&end=2026-09-11").text
check("custom range: both dates come back in the boxes and an Apply button is there to run it",
      'name="start" id="dtStart" value="2026-09-01"' in h and 'name="end" id="dtEnd" value="2026-09-11"' in h and 'id="dtApply"' in h)
check("the header says which range is shown", "Custom" in h)
check("a preset range hides the date boxes", 'id="customDates" style="display:none' in c.get("/status?range=7d").text)
check("the page still renders when custom arrives without dates", c.get("/status?range=custom").status_code == 200)

# ---- 3. the filter bar in a browser -----------------------------------------------
CHROME = os.environ.get("PW", "/opt/pw-browsers/chromium")
if not os.path.exists(CHROME):
    print("SKIP browser part (no Chromium at " + CHROME + ")")
else:
    import uvicorn
    port = 8841
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    U = f"http://127.0.0.1:{port}"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        pg = b.new_page(viewport={"width": 1500, "height": 900}); errs = []
        pg.on("pageerror", lambda ev: errs.append(str(ev)))
        pg.goto(U + "/login"); pg.fill("input[name=email]", config.OWNER_EMAIL)
        pg.fill("input[name=password]", config.APP_PASSWORD); pg.click("button[type=submit]"); pg.wait_for_load_state("networkidle")
        pg.goto(U + "/status"); pg.wait_for_load_state("networkidle")
        check("date boxes are hidden until Custom is chosen", pg.locator("#customDates").is_hidden())
        pg.locator('[data-fsel] button:has-text("Range")').click()
        pg.locator('[data-fsel] .fmenu a[data-val="custom"]').click(); pg.wait_for_timeout(150)
        check("choosing Custom reveals the boxes and does NOT reload with the old range",
              pg.locator("#customDates").is_visible() and "range=" not in urlsplit(pg.url).query)
        check("Apply is held back until both dates are filled", pg.locator("#dtApply").is_disabled())
        pg.fill("#dtStart", "2026-09-01"); pg.fill("#dtEnd", "2026-09-11"); pg.wait_for_timeout(100)
        check("…and enabled once they are", pg.locator("#dtApply").is_enabled())
        with pg.expect_navigation():
            pg.click("#dtApply")
        q = parse_qs(urlsplit(pg.url).query)
        check("Apply reloads the campaign list on that exact range",
              q.get("range") == ["custom"] and q.get("start") == ["2026-09-01"] and q.get("end") == ["2026-09-11"], pg.url)
        check("the chosen dates survive the reload", pg.input_value("#dtStart") == "2026-09-01" and pg.input_value("#dtEnd") == "2026-09-11")
        check("the end box opens on the start date", pg.get_attribute("#dtEnd", "min") == "2026-09-01")
        pg.fill("#dtEnd", "2026-09-05")
        with pg.expect_navigation():
            pg.keyboard.press("Enter")          # Enter in a date box applies too
        check("Enter in a date box applies as well", parse_qs(urlsplit(pg.url).query).get("end") == ["2026-09-05"], pg.url)
        # Custom chosen but dates not filled in, then another filter used: the range must not
        # silently become "today shown as Custom"
        pg.goto(U + "/status?range=7d"); pg.wait_for_load_state("networkidle")
        pg.locator('[data-fsel] button:has-text("Range")').click()
        pg.locator('[data-fsel] .fmenu a[data-val="custom"]').click(); pg.wait_for_timeout(120)
        with pg.expect_navigation():
            pg.locator("#stateSeg button:has-text('Paused')").click()
        q = parse_qs(urlsplit(pg.url).query)
        check("half-filled custom range doesn't hijack another filter — the previous range stays", q.get("range") == ["7d"] and q.get("state") == ["paused"], pg.url)
        pg.goto(U + "/status?range=custom&start=2026-09-01&end=2026-09-05"); pg.wait_for_load_state("networkidle")
        pg.locator('[data-fsel] button:has-text("Range")').click()
        with pg.expect_navigation():
            pg.locator('[data-fsel] .fmenu a[data-val="7d"]').click()
        q = parse_qs(urlsplit(pg.url).query)
        check("switching back to a preset applies at once and drops the custom dates", q.get("range") == ["7d"] and pg.locator("#customDates").is_hidden())
        check("no JS errors", not errs, str(errs)[:200])
        b.close()
    srv.should_exit = True

print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
