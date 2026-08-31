"""Public postback endpoint (Glitchy calls this) + the P&L page.

Postback URL (from /settings) — per-event style:
  /postback?key=<POSTBACK_KEY>&source={source}&revenue={payout}
           &txn={transaction_id}&event=purchase&ttclid={ttclid}

The old aggregate params (clicks/conversions/cvr) still work. Per-event
behavior: `txn` dedupes retries (same transaction never counts twice), and
when the TikTok Events API is enabled in Settings, every postback carrying a
`ttclid` is forwarded server-side to the pixel (`/event/track/`) so TikTok's
optimization learns from real purchases. Forwarding failures NEVER fail the
postback — the status is recorded on the event row instead.

Auth is the `key` query param — the endpoint is outside the login wall so
Glitchy's servers can reach it. Wrong/missing key -> 403, nothing stored.

P&L joins revenue-per-source (postbacks) against spend-per-source
(SpendSnapshot rows of campaigns launched with that source).
"""
from __future__ import annotations

from datetime import timezone as dt_timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import live_log, models, tiktok_api, timeutil
from ..database import get_db
from ..settings_store import get_settings
from ..templating import render

router = APIRouter()


def _events_pixel_code(db: Session, source: str, settings: dict) -> tuple[str, str]:
    """(access_token, pixel_code) to fire the Events API with, for a source.

    Settings override wins for the pixel; otherwise the source's most recent
    successful launch tells us the account, whose PixelCache entry (resolved
    at launch time — §9.7) carries the pixel code TikTok knows."""
    log = (db.query(models.LaunchLog)
           .filter(models.LaunchLog.ok == True,                     # noqa: E712
                   models.LaunchLog.source == source)
           .order_by(models.LaunchLog.id.desc()).first())
    token = ""
    pixel_code = (settings.get("events_pixel_code") or "").strip()
    if log:
        acct = (db.query(models.AdAccount)
                .filter_by(advertiser_id=log.advertiser_id).first())
        if acct and acct.access_token:
            token = acct.access_token
        if not pixel_code:
            cache = (db.query(models.PixelCache)
                     .filter_by(advertiser_id=log.advertiser_id)
                     .order_by(models.PixelCache.id.desc()).first())
            if cache:
                pixel_code = cache.pixel_code
    if not token:   # source unknown/never launched — any connected account can fire
        acct = (db.query(models.AdAccount)
                .filter(models.AdAccount.access_token != "").first())
        if acct:
            token = acct.access_token
    return token, pixel_code


def _forward_to_tiktok(db: Session, event: models.PostbackEvent, s: dict) -> str:
    """Fire the Events API for one stored postback. Returns a status string —
    never raises (a pixel hiccup must not bounce Glitchy's postback)."""
    if not s.get("events_api_enabled"):
        return ""
    if not event.ttclid:
        return "skipped: no ttclid on the postback"
    token, pixel_code = _events_pixel_code(db, event.source, s)
    if not token:
        return "error: no connected account token to fire with"
    if not pixel_code:
        return ("error: no pixel to fire to — set one in Settings → Events API, "
                "or launch this source once so it can be auto-resolved")
    try:
        tiktok_api.track_event(
            token, pixel_code,
            event=(s.get("events_event_name") or "CompleteRegistration").strip(),
            event_id=event.txn or f"pb{event.id}",
            ttclid=event.ttclid, value=float(event.revenue or 0),
            currency=(s.get("events_currency") or "USD").strip(),
            test_event_code=(s.get("events_test_code") or "").strip())
        return "sent"
    except tiktok_api.TikTokError as e:
        return f"error: code {e.code}: {(e.message or '')[:160]}"


def _num(v, cast=float, default=0):
    try:
        return cast(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return default


@router.get("/postback")
@router.post("/postback")
async def postback(request: Request, db: Session = Depends(get_db)):
    q = dict(request.query_params)
    s = get_settings(db)
    if q.get("key", "") != s["postback_key"]:
        return JSONResponse({"ok": False, "error": "bad key"}, status_code=403)
    source = (q.get("source") or "").strip()
    if not source:
        return JSONResponse({"ok": False, "error": "missing source"}, status_code=400)
    txn = (q.get("txn") or q.get("transaction_id") or "").strip()[:120]
    if txn:
        # retries/duplicates of the same transaction must never double revenue
        dupe = (db.query(models.PostbackEvent)
                .filter_by(source=source, txn=txn).first())
        if dupe:
            return {"ok": True, "duplicate": True}
    # per-event postbacks (txn present) count as 1 conversion unless told otherwise
    default_conv = 1 if txn else 0
    event = models.PostbackEvent(
        source=source,
        revenue=_num(q.get("revenue") or q.get("payout") or q.get("amount")),
        clicks=_num(q.get("clicks"), int),
        conversions=_num(q.get("conversions"), int, default_conv),
        cvr=_num(q.get("cvr")),
        txn=txn,
        ttclid=(q.get("ttclid") or q.get("click_id") or "").strip()[:500],
        event=(q.get("event") or "").strip()[:60],
        raw_query=str(request.url.query)[:2000],
    )
    if s["postback_mode"] == "snapshot":
        # replace today's totals for this source instead of accumulating
        day_start = timeutil.local_midnight_utc(0).replace(tzinfo=None)
        (db.query(models.PostbackEvent)
           .filter(models.PostbackEvent.source == source,
                   models.PostbackEvent.created_at >= day_start).delete())
    db.add(event)
    db.commit()
    # server-side pixel event (Events API) — best-effort, never bounces Glitchy
    status = _forward_to_tiktok(db, event, s)
    if status:
        event.forward_status = status
        db.commit()
    if event.revenue:
        live_log.push("conversion", f"Postback: {source} +${event.revenue:.2f}")
    out = {"ok": True}
    if status:
        out["events_api"] = status
    return out


# ---------------------------------------------------------------------------
# P&L page
# ---------------------------------------------------------------------------

def _spend_by_source(db: Session, start_utc, end_utc) -> dict[str, float]:
    """source -> spend, via LaunchLog (campaign→source) + SpendSnapshot."""
    camp_source = {}
    for log in (db.query(models.LaunchLog)
                .filter(models.LaunchLog.ok == True,  # noqa: E712
                        models.LaunchLog.source != "")):
        if log.campaign_id:
            camp_source[log.campaign_id] = log.source
    if not camp_source:
        return {}
    start_day = timeutil.local_date_str(start_utc)
    end_day = timeutil.local_date_str(end_utc)
    out: dict[str, float] = {}
    rows = (db.query(models.SpendSnapshot)
            .filter(models.SpendSnapshot.campaign_id.in_(list(camp_source)),
                    models.SpendSnapshot.day >= start_day,
                    models.SpendSnapshot.day <= end_day).all())
    for r in rows:
        src = camp_source.get(r.campaign_id, "")
        out[src] = out.get(src, 0.0) + float(r.spend or 0)
    return out


@router.get("/pnl")
def pnl(request: Request, db: Session = Depends(get_db)):
    range_key = request.query_params.get("range", "today")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    start_utc, end_utc = timeutil.range_bounds(range_key, start, end)
    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)

    rev_rows = (db.query(models.PostbackEvent.source,
                         func.sum(models.PostbackEvent.revenue).label("revenue"),
                         func.sum(models.PostbackEvent.conversions).label("conversions"),
                         func.sum(models.PostbackEvent.clicks).label("clicks"))
                .filter(models.PostbackEvent.created_at >= s_naive,
                        models.PostbackEvent.created_at < e_naive)
                .group_by(models.PostbackEvent.source).all())
    spend_by_src = _spend_by_source(db, start_utc, end_utc)

    sparks = db.query(models.SparkCode).filter(models.SparkCode.source != "").all()
    spark_by_src = {}
    for sp in sparks:
        spark_by_src.setdefault(sp.source, sp)

    sources = []
    seen = set()
    for r in rev_rows:
        seen.add(r.source)
        spend = spend_by_src.get(r.source, 0.0)
        revenue = float(r.revenue or 0)
        sp = spark_by_src.get(r.source)
        sources.append({"source": r.source, "revenue": revenue, "spend": spend,
                        "profit": revenue - spend,
                        "conversions": int(r.conversions or 0),
                        "clicks": int(r.clicks or 0),
                        "spark": sp.name if sp else "—"})
    for src, spend in spend_by_src.items():   # spend with no revenue yet
        if src not in seen:
            sp = spark_by_src.get(src)
            sources.append({"source": src, "revenue": 0.0, "spend": spend,
                            "profit": -spend, "conversions": 0, "clicks": 0,
                            "spark": sp.name if sp else "—"})
    sources.sort(key=lambda x: x["profit"], reverse=True)

    # roll up per spark code
    by_spark: dict[str, dict] = {}
    for row in sources:
        key = row["spark"]
        agg = by_spark.setdefault(key, {"spark": key, "revenue": 0.0, "spend": 0.0,
                                        "profit": 0.0, "sources": 0})
        agg["revenue"] += row["revenue"]
        agg["spend"] += row["spend"]
        agg["profit"] += row["profit"]
        agg["sources"] += 1
    spark_rows = sorted(by_spark.values(), key=lambda x: x["profit"], reverse=True)

    totals = {"revenue": sum(r["revenue"] for r in sources),
              "spend": sum(r["spend"] for r in sources)}
    totals["profit"] = totals["revenue"] - totals["spend"]
    totals["roas"] = (totals["revenue"] / totals["spend"]) if totals["spend"] else 0

    recent = (db.query(models.PostbackEvent)
              .order_by(models.PostbackEvent.created_at.desc()).limit(25).all())
    return render(request, "pnl.html", {
        "title": "P&L", "range_key": range_key, "start": start or "", "end": end or "",
        "sources": sources, "spark_rows": spark_rows, "totals": totals,
        "recent": recent,
    })
