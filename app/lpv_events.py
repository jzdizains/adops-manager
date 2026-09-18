"""Landing-view conversions (v127) — Settings › Events API › "On landing page view".

When a lander's VIEW beacon arrives (/t/lp) and the setting is on for that page, a
server-side Events API event (default CompleteRegistration, a fixed value) is fired for
the visit, with the same match signals the postback path sends: the TikTok click id, the
visitor id hashed as external_id, the visit's ip + user agent, the page URL. One event
per visitor per page (event_id = lpv-<page>-<visitor id>, so TikTok dedupes repeats).

Sent from ONE background thread through a bounded queue, so the beacon answers at once and
a flood cannot pile up threads or memory (over the cap the view is dropped, counted).
Nothing here changes what the page fires itself.

Operator's note (kept here on purpose): this reports a page view to TikTok as the
conversion event. The optimizer then learns from visits, not offer conversions; keep the
real conversions (the postback path) separate and watch lead quality.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime

MAX_QUEUE = 2000
STATS = {"queued": 0, "sent": 0, "failed": 0, "skipped": 0, "dropped": 0, "last": "", "last_at": None}
_q: "queue.Queue[dict]" = queue.Queue(maxsize=MAX_QUEUE)
_started = threading.Lock()
_thread: threading.Thread | None = None
_settings_cache: dict = {}          # source → (settings, at)
CACHE_SEC = 60


def pages_match(spec: str, page: str) -> bool:
    words = [w.strip().lower() for w in str(spec or "").split(",") if w.strip()]
    return not words or str(page or "").lower() in words


def wanted(d: dict) -> bool:
    """Cheap request-time gate: a VIEW with a visitor id and a TikTok click id (or our click id)."""
    return (str(d.get("step") or "") == "view" and bool(d.get("vid"))
            and isinstance(d.get("ttclid"), str) and len(d.get("ttclid") or "") > 1)


def enqueue(d: dict, ip: str, user_agent: str) -> bool:
    """Called by /t/lp after the beacon is stored. Never blocks, never raises."""
    if not wanted(d):
        return False
    item = {"page": str(d.get("page") or "")[:40], "source": str(d.get("source") or "")[:200], "vid": str(d.get("vid") or "")[:64],
            "ttclid": str(d.get("ttclid") or "")[:2000], "url": str(d.get("url") or "")[:900], "ref": str(d.get("ref") or "")[:400],
            "ip": ip or "", "ua": (user_agent or "")[:500]}
    try:
        _q.put_nowait(item)
    except queue.Full:
        STATS["dropped"] += 1
        return False
    STATS["queued"] += 1
    _ensure_worker()
    return True


def _ensure_worker() -> None:
    global _thread
    with _started:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name="lpv-events", daemon=True)
        _thread.start()


def _settings_for(db, source: str) -> dict:
    from . import settings_store
    from .routes.postback import _launch_for_source
    now = datetime.utcnow()
    hit = _settings_cache.get(source)
    if hit and (now - hit[1]).total_seconds() < CACHE_SEC:
        return hit[0]
    log = _launch_for_source(db, source) if source else None
    s = settings_store.for_account(db, log.advertiser_id) if log else settings_store.get_settings(db, None)
    if len(_settings_cache) > 500:
        _settings_cache.clear()
    _settings_cache[source] = (s, now)
    return s


def fire(db, item: dict) -> str:
    """One queued view → one Events API call. Returns a status string (sent / skipped / error)."""
    from . import tiktok_api, tracking
    from .routes.postback import _events_pixel_code, hash_id, page_url_for
    s = _settings_for(db, item["source"])
    if not s.get("lpv_enabled"):
        return "skipped: off"
    if not pages_match(s.get("lpv_pages", ""), item["page"]):
        return "skipped: page not listed"
    ttclid = item["ttclid"]
    if tracking.is_click_id(ttclid):                     # the kit's pages send our short click id when they have one
        click = tracking.lookup(db, ttclid)
        ttclid = click.ttclid if click else ""
    if not ttclid:
        return "skipped: no ttclid"
    token, pixel_code = _events_pixel_code(db, item["source"], s)
    if (s.get("events_access_token") or "").strip():
        token = s["events_access_token"].strip()
    if not token or not pixel_code:
        return "error: no token / pixel (Settings › Events API)"
    event = (s.get("lpv_event") or "CompleteRegistration").strip()
    try:
        tiktok_api.track_event(
            token, pixel_code, event=event, event_id=f"lpv-{item['page']}-{item['vid']}"[:256], ttclid=ttclid,
            value=float(s.get("lpv_value") or 0), currency=(s.get("events_currency") or "USD").strip(),
            test_event_code=(s.get("events_test_code") or "").strip(),
            ip=item["ip"], user_agent=item["ua"], external_id=hash_id(item["vid"]),
            page_url=item["url"] or page_url_for(db, item["source"], s), referrer=item["ref"])
        return f"sent {event}"
    except tiktok_api.TikTokError as e:
        return f"error: code {e.code}: {(e.message or '')[:160]}"
    except Exception as e:  # noqa: BLE001 — network hiccup: counted, never re-raised
        return f"error: {type(e).__name__}"


def _run() -> None:
    from .database import SessionLocal
    while True:
        item = _q.get()
        db = SessionLocal()
        try:
            status = fire(db, item)
        except Exception as e:  # noqa: BLE001
            status = f"error: {type(e).__name__}"
        finally:
            db.close()
        key = "sent" if status.startswith("sent") else ("skipped" if status.startswith("skipped") else "failed")
        STATS[key] += 1
        STATS["last"] = status
        STATS["last_at"] = datetime.utcnow()
        _q.task_done()
