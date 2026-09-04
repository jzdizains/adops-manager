"""Public postback endpoint (Glitchy calls this) + the P&L page.

Postback URL (from /settings) — per-event style:
  /postback?key=<POSTBACK_KEY>&source={source}&revenue={payout}
           &txn={transaction_id}&event=purchase

Glitchy only echoes {source} and {payout}, so the TikTok click id rides INSIDE
the source: the lander script sends Glitchy  source=<campaign name>~<ttclid>
and this endpoint splits it back — the name is what the P&L joins on, the
ttclid is what the Events API needs. A plain `ttclid=` param (if a network
ever offers one) still wins over the packed value.

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


# TikTok optimization events (ad group) -> Events API standard web event names.
# The event TikTok's algorithm learns from must be the one the ad group optimises
# for, so a split test (registration vs purchase campaigns side by side) needs each
# postback to fire ITS campaign's event, not one global name.
# Event names as the Events API 2.0 "Web Standard Events" table spells them
# (case-sensitive). The purchase event is "Purchase" there — "CompletePayment"
# is the older browser-pixel name and is NOT in the 2.0 list.
OPT_EVENT_TO_WEB_EVENT = {
    "ON_WEB_REGISTER": "CompleteRegistration",
    "SHOPPING": "Purchase",
    "ON_WEB_ORDER": "PlaceAnOrder",
    "FORM": "SubmitForm",
    "ON_WEB_DETAIL": "ViewContent",
    "BUTTON": "ClickButton",
}
LEGACY_EVENT_NAMES = {"CompletePayment": "Purchase"}   # normalise old pixel names typed into Settings


def _launch_for_source(db: Session, source: str):
    """The most recent successful launch that carries this source (campaign name)."""
    return (db.query(models.LaunchLog)
            .filter(models.LaunchLog.ok == True,                     # noqa: E712
                    models.LaunchLog.source == source)
            .order_by(models.LaunchLog.id.desc()).first())


def event_name_for(db: Session, source: str, settings: dict, log=None) -> tuple[str, str]:
    """(event name to fire, how it was chosen). Campaign mode fires the event the
    campaign's ad group optimises for; the fixed name is the fallback when the
    launch didn't record one (older launches, non-pixel destinations)."""
    fixed = (settings.get("events_event_name") or "CompleteRegistration").strip()
    fixed = LEGACY_EVENT_NAMES.get(fixed, fixed)
    if settings.get("events_event_mode", "campaign") != "campaign":
        return fixed, "fixed"
    log = log or _launch_for_source(db, source)
    opt = (getattr(log, "optimization_event", "") or "") if log else ""
    ev = OPT_EVENT_TO_WEB_EVENT.get(opt, "")
    if ev:
        return ev, f"campaign optimises for {opt}"
    return fixed, "fallback (launch has no optimization event)"


def page_url_for(db: Session, source: str, settings: dict) -> str:
    """page.url is REQUIRED on web events (Events API 2.0). Use the landing
    URL the source's campaign launched with, else the URL from Settings, else
    a bare https:// so the event is still well-formed."""
    log = _launch_for_source(db, source) if source else None
    url = (log.landing_url if log and log.landing_url else "") or (settings.get("events_page_url") or "").strip()
    return url or "https://unknown.landing/"


def _events_pixel_code(db: Session, source: str, settings: dict) -> tuple[str, str]:
    """(access_token, pixel_code) to fire the Events API with, for a source.

    Settings override wins for the pixel; otherwise the source's most recent
    successful launch tells us the account, whose PixelCache entry (resolved
    at launch time — §9.7) carries the pixel code TikTok knows."""
    log = _launch_for_source(db, source)
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
    # a dedicated Events API token (from Events Manager) always wins — the
    # Marketing API token often lacks the events permission (code 40001)
    if (s.get("events_access_token") or "").strip():
        token = s["events_access_token"].strip()
    if not token:
        return "error: no connected account token to fire with"
    if not pixel_code:
        return ("error: no pixel to fire to — set one in Settings → Events API, "
                "or launch this source once so it can be auto-resolved")
    event_name, _how = event_name_for(db, event.source, s)
    try:
        tiktok_api.track_event(
            token, pixel_code,
            event=event_name,
            event_id=event.txn or f"pb{event.id}",
            ttclid=event.ttclid, value=float(event.revenue or 0),
            currency=(s.get("events_currency") or "USD").strip(),
            test_event_code=(s.get("events_test_code") or "").strip(),
            page_url=page_url_for(db, event.source, s))
        return f"sent {event_name}"
    except tiktok_api.TikTokError as e:
        return f"error: code {e.code}: {(e.message or '')[:160]}"


UNATTRIBUTED = "(unattributed)"   # postbacks whose {source} macro came through empty
SOURCE_SEP = "~"                  # source=<name>~<ttclid> — see module docstring / pass-source.js


def unpack_source(raw: str) -> tuple[str, str]:
    """'camp_a1234~E.C.P.abc' -> ('camp_a1234', 'E.C.P.abc'); plain -> (plain, '').
    Split on the FIRST separator: names can't contain it, a click id might."""
    raw = (raw or "").strip()
    if SOURCE_SEP not in raw:
        return raw, ""
    name, packed = raw.split(SOURCE_SEP, 1)
    return name.strip(), packed.strip()


def _is_macro(v: str) -> bool:
    """True for an UNREPLACED macro the network sent literally: '{ttclid}',
    '{transaction_id}', '__CLICKID__'."""
    v = (v or "").strip()
    return bool(v) and ("{" in v or "}" in v or (v.startswith("__") and v.endswith("__")))


def _clean_ttclid(v: str) -> str:
    """An unreplaced macro must never be stored as a click id — TikTok would
    just reject the event."""
    v = (v or "").strip()
    return "" if (not v or _is_macro(v)) else v[:500]


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
    source, packed_ttclid = unpack_source(q.get("source") or "")
    ttclid = _clean_ttclid(q.get("ttclid") or q.get("click_id") or "") or _clean_ttclid(packed_ttclid)
    if not source:
        # Glitchy fired but its {source} macro was EMPTY — the click that reached
        # Glitchy never carried ?source=. Keep the revenue (as unattributed) and
        # make the failure visible instead of silently discarding it.
        source = UNATTRIBUTED
    txn = (q.get("txn") or q.get("transaction_id") or "").strip()[:120]
    per_event = bool(txn) or bool((q.get("event") or "").strip())
    if _is_macro(txn):
        # the network has no such macro and sent it literally — deduping on it
        # would swallow every conversion after the first one for this source
        txn = ""
    if txn:
        # retries/duplicates of the same transaction must never double revenue
        dupe = (db.query(models.PostbackEvent)
                .filter_by(source=source, txn=txn).first())
        if dupe:
            return {"ok": True, "duplicate": True}
    # per-event postbacks (txn or event present) count as 1 conversion unless told otherwise
    default_conv = 1 if per_event else 0
    event = models.PostbackEvent(
        source=source,
        revenue=_num(q.get("revenue") or q.get("payout") or q.get("amount")),
        clicks=_num(q.get("clicks"), int),
        conversions=_num(q.get("conversions"), int, default_conv),
        cvr=_num(q.get("cvr")),
        txn=txn,
        ttclid=ttclid,
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


GOAL_LABELS = {"ON_WEB_REGISTER": "Registration", "SHOPPING": "Purchase", "ON_WEB_ORDER": "Place order",
               "FORM": "Form", "ON_WEB_DETAIL": "View content", "BUTTON": "Button"}


def _goals_by_source(db: Session, sources: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not sources:
        return out
    for lg in (db.query(models.LaunchLog)
               .filter(models.LaunchLog.ok == True, models.LaunchLog.source.in_(sources))  # noqa: E712
               .order_by(models.LaunchLog.id.asc())):
        if lg.optimization_event:
            out[lg.source] = GOAL_LABELS.get(lg.optimization_event, lg.optimization_event)
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
        # spark-code roll-up only means something in static-source mode
        "has_spark": any(r["spark"] != "—" for r in sources),
        # which pixel event each source's campaign optimises for (split-test view)
        "goal_by_src": _goals_by_source(db, [r["source"] for r in sources]),
    })
