"""In-app browser escape test — measure, on YOUR traffic, whether TikTok's
in-app browser lets a visitor be moved to their real browser.

How it works (three public pages the phone talks to, one results page for you):

  /t/escape            the page you point a small test ad at. It records the
                       phone (platform, in-app or not, TikTok version) and shows
                       ONE big button. The tap tries the escape — Android:
                       Chrome intent; iPhone: x-safari- scheme — then, if the
                       page is still visible ~1.2s later, continues in-app.
  /t/escape/ping       beacon: "opened" / "clicked <method>"
  /t/escape/landed     where every visitor ends up (escaped OR in-app fallback).
                       Its user agent is the proof: a TikTok WebView UA means
                       the visitor is still inside the app; a plain Safari /
                       Chrome UA means the escape worked.
  /escape-test         (login) counts by platform × outcome + the raw rows.

Nothing here touches campaigns or spend; it's a plain page you can also open
from any phone by hand (Copy the link → paste into a TikTok DM → tap it).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..templating import render

router = APIRouter()

TIKTOK_UA = re.compile(r"TikTok|musical_ly|Bytedance|BytedanceWebview|ByteLocale|trill", re.I)
VERSION_UA = re.compile(r"(?:musical_ly|TikTok|trill)[_/ ]?v?(\d+(?:\.\d+)+)", re.I)


def classify(ua: str) -> dict:
    """What kind of browser sent this user agent."""
    ua = ua or ""
    if re.search(r"iPhone|iPad|iPod", ua):
        platform = "ios"
    elif re.search(r"Android", ua):
        platform = "android"
    else:
        platform = "other"
    inapp = bool(TIKTOK_UA.search(ua))
    m = VERSION_UA.search(ua)
    return {"platform": platform, "inapp": inapp, "app_version": m.group(1) if m else ""}


def _base(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    return base


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Continue</title>
<style>
html,body{margin:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0d0f15;color:#e7e9f1}
.wrap{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px 24px;text-align:center;gap:18px}
h1{font-size:26px;margin:0;letter-spacing:-.02em}p{margin:0;color:#b6bccc;font-size:15px;line-height:1.5;max-width:420px}
button{font:inherit;font-size:19px;font-weight:700;padding:18px 36px;border-radius:999px;border:0;background:#8a8af0;color:#fff;width:100%;max-width:360px;box-shadow:0 10px 30px rgba(0,0,0,.35)}
button:active{transform:scale(.97)}.tiny{font-size:12px;color:#666d84}
</style></head><body><div class="wrap">
<h1>Get started here</h1>
<p>Tap continue to open the offer.</p>
<button id="go">Continue</button>
<p class="tiny" id="dbg"></p>
</div>
<script>
(function(){
  var UA=navigator.userAgent||"", V=__VISIT__, BASE=__BASE__;
  var isIOS=/iPhone|iPad|iPod/i.test(UA), isAndroid=/Android/i.test(UA);
  var inApp=/TikTok|musical_ly|Bytedance|BytedanceWebview|ByteLocale|trill/i.test(UA);
  var target=BASE+"/t/escape/landed?v="+V;
  function ping(ev,extra){try{navigator.sendBeacon(BASE+"/t/escape/ping",JSON.stringify({v:V,ev:ev,ua:UA,extra:extra||""}));}catch(e){}}
  ping("opened");
  document.getElementById("dbg").textContent=(inApp?"TikTok in-app browser":"regular browser")+" · "+(isIOS?"iPhone":isAndroid?"Android":"other");
  document.getElementById("go").addEventListener("click",function(){
    var method = !inApp ? "direct" : (isAndroid ? "intent" : (isIOS ? "x-safari" : "direct"));
    ping("clicked", method);
    if(method==="direct"){ location.href=target; return; }
    var left=false, gone=function(){left=true;};
    document.addEventListener("visibilitychange",function(){ if(document.visibilityState==="hidden") gone(); });
    window.addEventListener("pagehide",gone); window.addEventListener("blur",gone);
    if(method==="intent"){
      location.href="intent://"+target.replace(/^https?:\\/\\//,"")+"#Intent;scheme=https;package=com.android.chrome;S.browser_fallback_url="+encodeURIComponent(target)+";end";
    } else {
      location.href="x-safari-"+target;
    }
    setTimeout(function(){ if(!left && document.visibilityState!=="hidden"){ ping("fallback", method); location.href=target; } },1200);
  });
})();
</script></body></html>"""

LANDED = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Thanks</title>
<style>html,body{margin:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0d0f15;color:#e7e9f1}
.wrap{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px 24px;text-align:center;gap:12px}
h1{font-size:24px;margin:0}p{margin:0;color:#b6bccc;font-size:14px;line-height:1.5;max-width:420px}
.pill{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:700;font-size:14px;background:__COLOR__;color:#fff}</style></head>
<body><div class="wrap"><h1>Test complete</h1><span class="pill">__LABEL__</span>
<p>__NOTE__</p><p style="font-size:12px;color:#666d84">You can close this page.</p></div></body></html>"""


@router.get("/t/escape", response_class=HTMLResponse)
def escape_page(request: Request, db: Session = Depends(get_db)):
    visit = secrets.token_hex(6)
    info = classify(request.headers.get("user-agent", ""))
    db.add(models.EscapeTest(visit=visit, platform=info["platform"], inapp=info["inapp"],
                             app_version=info["app_version"], ua_open=request.headers.get("user-agent", "")[:500],
                             outcome="no-click"))
    db.commit()
    import json
    html = PAGE.replace("__VISIT__", json.dumps(visit)).replace("__BASE__", json.dumps(_base(request)))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.post("/t/escape/ping")
async def escape_ping(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
    except Exception:
        try:
            import json
            data = json.loads((await request.body()).decode("utf-8", "ignore") or "{}")
        except Exception:
            return JSONResponse({"ok": False}, status_code=400)
    row = db.query(models.EscapeTest).filter_by(visit=str(data.get("v") or "")).first()
    if not row:
        return JSONResponse({"ok": False}, status_code=404)
    ev = str(data.get("ev") or "")
    if ev == "clicked":
        row.clicked = True
        row.method = str(data.get("extra") or "")[:20]
        if row.outcome == "no-click":
            row.outcome = "lost"          # until the landed page says otherwise
    elif ev == "fallback":
        row.method = (row.method or str(data.get("extra") or "")[:20]) + "+fallback"
    db.commit()
    return {"ok": True}


@router.get("/t/escape/landed", response_class=HTMLResponse)
def escape_landed(request: Request, db: Session = Depends(get_db)):
    visit = request.query_params.get("v", "")
    ua = request.headers.get("user-agent", "")
    info = classify(ua)
    row = db.query(models.EscapeTest).filter_by(visit=visit).first()
    outcome = "stayed" if info["inapp"] else "escaped"
    if not row:
        # your OWN prelander pointed here (v=prelander or anything unknown): record
        # the arrival as a row of its own so the results page shows it too
        row = models.EscapeTest(visit=(visit or "manual")[:40], platform=info["platform"], inapp=info["inapp"],
                                app_version=info["app_version"], ua_open="", method=f"from:{(visit or 'manual')[:12]}",
                                clicked=True, outcome=outcome)
        db.add(row)
    if row:
        row.landed_at = datetime.now(timezone.utc)
        # both can fire (Safari opened AND the WebView's fallback ran) — an escape
        # is never downgraded to "stayed" by the in-app copy arriving second
        if not (row.outcome == "escaped" and outcome == "stayed"):
            row.ua_landed = ua[:500]
            row.outcome = outcome
        if not row.clicked:
            row.clicked = True            # the beacon can lose the race with navigation
        db.commit()
    label = "Opened in your real browser ✓" if outcome == "escaped" else "Still inside the TikTok browser"
    note = ("The escape worked on this phone." if outcome == "escaped"
            else "TikTok's in-app browser kept this visit inside the app — the fallback continued here.")
    color = "#0e9f6e" if outcome == "escaped" else "#b9770a"
    return HTMLResponse(LANDED.replace("__LABEL__", label).replace("__NOTE__", note).replace("__COLOR__", color),
                        headers={"Cache-Control": "no-store"})


@router.get("/escape-test")
def escape_results(request: Request, db: Session = Depends(get_db)):
    rows = (db.query(models.EscapeTest).order_by(models.EscapeTest.created_at.desc()).limit(500).all())
    # summary: platform × outcome, in-app opens only (a regular-browser open proves nothing)
    summary: dict[str, dict[str, int]] = {}
    for r in rows:
        if not r.inapp:
            continue
        s = summary.setdefault(r.platform, {"opened": 0, "clicked": 0, "escaped": 0, "stayed": 0, "lost": 0})
        s["opened"] += 1
        if r.clicked:
            s["clicked"] += 1
        if r.outcome in ("escaped", "stayed", "lost"):
            s[r.outcome] += 1
    versions = sorted({r.app_version for r in rows if r.app_version})
    return render(request, "escape_results.html", {
        "title": "In-app escape test", "rows": rows, "summary": summary, "versions": versions,
        "test_url": _base(request) + "/t/escape",
        "not_inapp": sum(1 for r in rows if not r.inapp),
    })


@router.post("/escape-test/clear")
def escape_clear(db: Session = Depends(get_db)):
    from fastapi.responses import RedirectResponse
    db.query(models.EscapeTest).delete()
    db.commit()
    return RedirectResponse("/escape-test?ok=Results+cleared", status_code=303)
