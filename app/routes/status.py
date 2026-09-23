"""Campaigns manager — every campaign across every account with on/off toggle,
full TikTok metrics (spend, impressions, clicks, CTR, CPC, CPM, conversions,
CPA), Glitchy postback performance (revenue, clicks, CVR, profit) joined per
campaign through its source, filters, sortable columns, and a manual sync.

Postback attribution note: Glitchy reports per SOURCE (one source = one spark
code). When the same spark was launched to several accounts, all those
campaigns share one source — revenue/clicks are apportioned by each campaign's
share of the source's spend (even split when nothing has spent yet), and the
row is marked 'split ÷N'. CVR is shown at source level (a ratio survives the
split unchanged)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from datetime import datetime, timezone

from .. import adgroup_stats, appeals as appeals_mod, board_numbers, live_spend, models, pnl_data, queries, tags as tags_mod, tiktok_api, timeutil
from ..database import get_db
from ..templating import render

from . import guard
from .. import scope as scope_mod

router = APIRouter()

# sort key -> how to read the value from a built row dict
SORT_KEYS = {
    "spend": lambda row: row["m"]["spend"],
    "impr": lambda row: row["m"]["impressions"],
    "clicks": lambda row: row["m"]["clicks"],
    "ctr": lambda row: row["m"]["ctr"],
    "cpc": lambda row: row["m"]["cpc"],
    "cpm": lambda row: row["m"]["cpm"],
    "conv": lambda row: row["m"]["conversions"],
    "cpa": lambda row: row["m"]["cpa"],
    "budget": lambda row: row["r"].budget,
    "name": lambda row: (row["r"].campaign_name or "").lower(),
    "source": lambda row: row["source"],
    "revenue": lambda row: row["revenue"],
    "pb_clicks": lambda row: row["pb_clicks"],
    "cvr": lambda row: row["cvr"],
    "profit": lambda row: row["profit"],
    "roas": lambda row: row["roas"],
    "epc": lambda row: row["epc"],
}
GROUPS = ("", "creative", "account", "bc", "source")

OBJECTIVE_LABELS = {"WEB_CONVERSIONS": "Website conversions", "CONVERSIONS": "Website conversions", "TRAFFIC": "Traffic",
                    "LEAD_GENERATION": "Lead generation", "APP_PROMOTION": "App promotion", "REACH": "Reach",
                    "VIDEO_VIEWS": "Video views", "ENGAGEMENT": "Community interaction", "PRODUCT_SALES": "Product sales"}


def objective_label(o: str) -> str:
    return OBJECTIVE_LABELS.get(o or "", (o or "Unknown").replace("_", " ").title())


def _hidden_key(sc) -> str:
    """Campaigns a user hid from their board (v155) — per workspace, like hidden profiles."""
    return f"hidden_campaigns:{sc.user_id if sc.user_id is not None else 'all'}"


def hidden_campaigns(db, sc) -> set:
    import json as _j
    try:
        return {str(x) for x in _j.loads(queries.get_setting(db, _hidden_key(sc), "[]") or "[]")}
    except (ValueError, TypeError):
        return set()


def board_filter(r, *, hidden: set, show_hidden: bool, obj: str, offer: str, sources: dict, warm: set) -> bool:
    """The v155 board filters for one campaign record. Pure."""
    if (r.campaign_id in hidden) != show_hidden:
        return False
    if obj and (r.objective_type or "") != obj:
        return False
    if offer == "none" and sources.get(r.campaign_id):
        return False
    if r.campaign_id in warm:
        return False
    return True


def leaderboard(rows: list, owner_by_adv: dict, names: dict) -> list:
    """Per buyer (the ad account's owner): spend, revenue, profit, ROAS, campaigns. Pure."""
    agg: dict = {}
    for row in rows:
        uid = owner_by_adv.get(row["r"].advertiser_id)
        a = agg.setdefault(uid, {"user_id": uid, "name": names.get(uid, "Unassigned accounts"), "spend": 0.0,
                                 "revenue": 0.0, "n": 0, "active": 0})
        a["spend"] += float(row["m"]["spend"] or 0)
        a["revenue"] += float(row["revenue"] or 0)
        a["n"] += 1
        a["active"] += 1 if row["r"].operation_status == "ENABLE" else 0
    out = []
    for a in agg.values():
        a["profit"] = round(a["revenue"] - a["spend"], 2)
        a["roas"] = round(a["revenue"] / a["spend"], 2) if a["spend"] else 0.0
        a["spend"], a["revenue"] = round(a["spend"], 2), round(a["revenue"], 2)
        out.append(a)
    return sorted(out, key=lambda a: (-a["profit"], -a["spend"]))


# Campaign secondary statuses that mean "switched on but CANNOT deliver"
# (Enumeration – Campaign Status – Secondary Status): the ad account is
# punished / failed review / contract pending, or the campaign itself was
# disapproved / paused by TikTok because its ad groups were rejected.
BLOCKED_CAMPAIGN_STATUSES = {
    "ADVERTISER_ACCOUNT_PUNISH", "CAMPAIGN_STATUS_ADVERTISER_ACCOUNT_PUNISH",
    "CAMPAIGN_STATUS_ADVERTISER_AUDIT_DENY", "CAMPAIGN_STATUS_ADVERTISER_AUDIT",
    "ADVERTISER_CONTRACT_PENDING", "CAMPAIGN_STATUS_ADVERTISER_CONTRACT_PENDING",
    "CAMPAIGN_STATUS_REVIEW_DISAPPROVED", "CAMPAIGN_STATUS_AD_UNAVAILABLE",
}
# Advertiser statuses (Enumeration – Advertiser Status) under which nothing delivers
BLOCKED_ACCOUNT_TOKENS = ("PUNISH", "LIMIT", "DISABLE", "CONFIRM_FAIL", "PENDING")


def blocked_reason(rec, acct) -> str:
    """Why a campaign that is switched ON is not actually delivering — '' when it
    can. Used to keep the Active view to campaigns that really run."""
    sec = (rec.secondary_status or "").upper()
    if sec in BLOCKED_CAMPAIGN_STATUSES or "PUNISH" in sec or "AUDIT_DENY" in sec:
        return sec.replace("CAMPAIGN_STATUS_", "").replace("_", " ").lower()
    ast = ((acct.status if acct else "") or "").upper()
    if ast and "ENABLE" not in ast and any(t in ast for t in BLOCKED_ACCOUNT_TOKENS):
        return "account " + ast.replace("STATUS_", "").replace("_", " ").lower()
    return ""


def campaign_health(r, acct, groups: list[dict]) -> dict:
    """One health per campaign from its ad groups' current statuses (app/health.py), plus
    how long it has been in that state and the ad-group mix ("16 delivering · 4 in review")."""
    from .. import health as health_mod
    st, detail = health_mod.derive((acct.status if acct else "") or "", groups, r.operation_status or "",
                                   r.budget_mode or "", r.budget or 0.0)
    h = health_mod.explain(st, detail)
    matching = [g["since"] for g in groups if g.get("since") and detail and g.get("secondary_status") == detail]
    anyt = [g["since"] for g in groups if g.get("since")]
    h["since"] = min(matching) if matching else (max(anyt) if anyt else None)
    h["mix"] = health_mod.mix_label(health_mod.mix(groups)) if len(groups) > 1 else ""
    return h


def bucket_of(r, acct, h: dict | None) -> tuple[str, str]:
    """(tab, blocked text) — ONE rule for the row filter and the tab counts.
    Paused = switched off; Blocked = on but can't deliver (TikTok's campaign/account flags,
    or a health that means it: rejected, out of money, account suspended); Pending = waiting
    on TikTok's review or its start time; Active = everything else that's on."""
    if r.operation_status != "ENABLE":
        return ("paused" if r.operation_status == "DISABLE" else "other"), ""
    blocked = blocked_reason(r, acct)
    if blocked:
        return "blocked", blocked
    b = (h or {}).get("bucket") or "active"
    return (b if b in ("active", "pending", "blocked", "paused") else "active"), ""


def _acct_level(blocked: str) -> bool:
    """A block that comes from the ad ACCOUNT (suspended / punished), not the campaign itself."""
    b = (blocked or "").lower()
    return b.startswith("account ") or "punish" in b or "advertiser" in b


def _naive(d):
    return d.astimezone(timezone.utc).replace(tzinfo=None) if getattr(d, "tzinfo", None) else d


def _health_json(rec, acct, db: Session) -> dict:
    h = campaign_health(rec, acct, adgroup_stats.states_for(db, [rec.campaign_id]).get(rec.campaign_id, []))
    since = h.pop("since", None)
    return {**h, "since": (since.isoformat() + "Z") if since else "", "since_ago": _ago(since) if since else ""}


@router.get("/status")
def status_page(request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)                 # whose campaigns (v116)
    q = request.query_params.get("q", "").strip().lower()
    state = request.query_params.get("state", "active")       # active (default) | pending | blocked | paused | all
    if state not in ("active", "pending", "blocked", "paused", "all"):
        state = "active"
    account = request.query_params.get("account", "")          # advertiser_id
    source_f = request.query_params.get("source", "").strip()  # P&L source filter
    origin = request.query_params.get("origin", "tool")        # tool | all
    ag_flags = adgroup_stats.flagged(db)        # campaigns whose numbers come from ACTIVE ad groups only
    range_key = request.query_params.get("range", "today")
    start = request.query_params.get("start") or None
    end = request.query_params.get("end") or None
    if range_key not in ("today", "yesterday", "7d", "30d", "mtd", "custom"):
        range_key = "today"
    sort = request.query_params.get("sort", "spend")
    if sort not in SORT_KEYS:
        sort = "spend"
    group = request.query_params.get("group", "")
    if group not in GROUPS:
        group = ""
    show_hidden = request.query_params.get("hidden") == "1"      # v155: the campaigns you hid, only
    obj_f = request.query_params.get("obj", "").strip()             # objective
    offer_f = request.query_params.get("offer", "")                 # "none" = no offer (source) matched
    nowarm = request.query_params.get("nowarm") == "1"              # hide warm-up (test) launches
    hidden_set = hidden_campaigns(db, sc)

    records = db.query(models.CampaignRecord).all()
    # campaigns this tool launched (successful launches carry the campaign id)
    tool_campaign_ids = {log.campaign_id for log in
                         db.query(models.LaunchLog.campaign_id, models.LaunchLog.advertiser_id)
                         .filter(models.LaunchLog.ok == True,          # noqa: E712
                                 models.LaunchLog.campaign_id != "",
                                 ~models.LaunchLog.campaign_id.startswith("deleted:"))
                         if sc.allows(log.advertiser_id)}
    cached_ids = {r.campaign_id for r in records}
    pending_ids = tool_campaign_ids - cached_ids         # launched, not synced yet
    pending_tool = len(pending_ids)
    pending_details: list[dict] = []
    if pending_ids:
        import json as _json
        try:
            report = _json.loads(queries.get_setting(db, "campaign_sync_report") or "{}")
        except (ValueError, TypeError):
            report = {}
        err_by_acct = {e.get("advertiser_id"): e for e in report.get("errors", [])}
        acct_names = {a.advertiser_id: (a.advertiser_name or a.advertiser_id)
                      for a in db.query(models.AdAccount).all()}
        for log_row in (db.query(models.LaunchLog)
                        .filter(models.LaunchLog.campaign_id.in_(list(pending_ids)))):
            e = err_by_acct.get(log_row.advertiser_id)
            pending_details.append({
                "campaign_id": log_row.campaign_id,
                "account": acct_names.get(log_row.advertiser_id, log_row.advertiser_id),
                "sync_error": (f"code {e['code']}: {e['message']}" if e else ""),
            })
    all_records = records
    if origin == "tool":
        records = [r for r in records if r.campaign_id in tool_campaign_ids]
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    older = request.query_params.get("older") == "1"      # Blocked: also errors from before the range
    issue_at = board_numbers.issue_dates(db, models)       # when each problem was first seen (errors-since)
    sources = pnl_data.campaign_source_map(db)
    warm_ids = ({c for (c,) in db.query(models.LaunchLog.campaign_id).filter(models.LaunchLog.warmup == True)}   # noqa: E712
                if nowarm else set())
    _bf = dict(hidden=hidden_set, show_hidden=show_hidden, obj=obj_f, offer=offer_f, sources=sources, warm=warm_ids)
    ag_states = adgroup_stats.states_for(db, [r.campaign_id for r in records if sc.allows(r.advertiser_id)])
    healths = {r.campaign_id: campaign_health(r, accounts.get(r.advertiser_id), ag_states.get(r.campaign_id, []))
               for r in records if sc.allows(r.advertiser_id)}

    # --- Glitchy postback truth for the selected range, per source -------------
    start_utc, end_utc = timeutil.range_bounds(range_key, start, end)
    pb = pnl_data.revenue_by_source(db, start_utc, end_utc)

    # --- TikTok metrics for the range: today = the synced cache (fast, free);
    #     any other range = one live report call per account in view ----------
    from datetime import timedelta as _td

    from .. import tiktok_api
    range_errors = 0
    metrics_by_cid: dict | None = None
    range_from_db = False
    if range_key != "today":
        metrics_by_cid = {}
        s_day = timeutil.local_date_str(start_utc)
        e_day = timeutil.local_date_str(end_utc - _td(seconds=1))
        in_view = {r.advertiser_id for r in records if sc.allows(r.advertiser_id)}
        covered: set = set()
        if request.query_params.get("live") != "1":
            # v148: the sweep's saved day rows — no TikTok call; only accounts with no saved
            # day in the range are still asked live
            from .. import range_db
            db_metrics, covered = range_db.metrics(db, models, list(in_view), s_day, e_day)
            metrics_by_cid.update({cid: {"_db": True, **m} for cid, m in db_metrics.items()})
            range_from_db = bool(covered)
        for aid in in_view - covered:
            a = accounts.get(aid)
            if not a or not a.access_token:
                continue
            try:
                for rr in tiktok_api.get_report(
                        a.access_token, aid, dimensions=["campaign_id"],
                        metrics=live_spend.REPORT_METRICS,
                        start_date=s_day, end_date=e_day):
                    cid = str(rr.get("dimensions", {}).get("campaign_id", ""))
                    metrics_by_cid[cid] = rr.get("metrics", {}) or {}
            except tiktok_api.TikTokError:
                range_errors += 1

    def _metrics(rec) -> dict:
        if metrics_by_cid is None:      # today → cached values
            return {"spend": float(rec.spend_today or 0), "impressions": rec.impressions or 0,
                    "clicks": rec.clicks or 0, "conversions": rec.conversions or 0,
                    "ctr": rec.ctr or 0.0, "cpc": rec.cpc or 0.0,
                    "cpm": rec.cpm or 0.0, "cpa": rec.cpa or 0.0}
        mm = metrics_by_cid.get(rec.campaign_id, {})
        if mm.get("_db"):              # already in the page's shape (range_db.combine)
            return {k: mm[k] for k in ("spend", "impressions", "clicks", "conversions", "ctr", "cpc", "cpm", "cpa")}
        f = live_spend._f
        return {"spend": f(mm, "spend"), "impressions": int(f(mm, "impressions")),
                "clicks": int(f(mm, "clicks")), "conversions": int(f(mm, "conversion")),
                "ctr": f(mm, "ctr"), "cpc": f(mm, "cpc"),
                "cpm": f(mm, "cpm"), "cpa": f(mm, "cost_per_conversion")}

    # how many campaigns share each source + that source's total spend in range
    # (computed over ALL records so filters never change the apportioning)
    src_count: dict[str, int] = {}
    src_spend: dict[str, float] = {}
    metrics_cache: dict[str, dict] = {}
    ag_metrics: dict[str, dict] = {}
    ag_revenue: dict[str, dict] = {}
    _ids = [r.campaign_id for r in records if r.campaign_id in ag_flags]
    if _ids:
        _s_day = timeutil.local_date_str(start_utc)
        _e_day = timeutil.local_date_str(end_utc - _td(seconds=1))
        ag_metrics = adgroup_stats.active_metrics(db, _ids, _s_day, _e_day)
        ag_revenue = adgroup_stats.active_revenue(db, _ids, start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None),
                                                  {c: sources.get(c, "") for c in _ids})
    for r in records:
        am = ag_metrics.get(r.campaign_id)
        # a flagged campaign with no ad-group rows yet (first sync pending) keeps its full numbers
        metrics_cache[r.campaign_id] = am if (am and am.get("has_rows")) else _metrics(r)
        src = sources.get(r.campaign_id, "")
        if src:
            src_count[src] = src_count.get(src, 0) + 1
            src_spend[src] = src_spend.get(src, 0.0) + metrics_cache[r.campaign_id]["spend"]

    rows = []
    for r in records:
        if not sc.allows(r.advertiser_id):          # another user's account — shares above still count it
            continue
        acct = accounts.get(r.advertiser_id)
        name = (acct.advertiser_name if acct else r.advertiser_id) or r.advertiser_id
        if q and q not in r.campaign_name.lower() and q not in name.lower():
            continue
        h = healths.get(r.campaign_id)
        tab, blocked = bucket_of(r, acct, h)
        if state != "all" and tab != state:
            continue
        err_at = None
        if tab == "blocked":
            # errors are dated: with a range picked, only the ones that started in it (the week-old
            # suspensions stay one click away — "N older")
            err_at = board_numbers.error_at(h, acct, _acct_level(blocked), r.campaign_id, issue_at)
            if state == "blocked" and not older and err_at is not None and err_at < start_utc.replace(tzinfo=None):
                continue
        if account and r.advertiser_id != account:
            continue
        if source_f and sources.get(r.campaign_id, "") != source_f:
            continue
        if not board_filter(r, **_bf):
            continue

        m = metrics_cache[r.campaign_id]
        src = sources.get(r.campaign_id, "")
        src_pb = pb.get(src, {}) if src else {}
        n = src_count.get(src, 1)
        ag_on = r.campaign_id in ag_flags
        ag_live = ag_on and bool((ag_metrics.get(r.campaign_id) or {}).get("has_rows"))
        if ag_live:
            # revenue by ad group id (ClickFlare field 6) — no spend-share apportioning
            ar = ag_revenue.get(r.campaign_id, {})
            src_pb = {"revenue": ar.get("revenue", 0.0), "conversions": ar.get("conversions", 0), "clicks": 0}
            n = 1
        if src and n > 1:
            total = src_spend.get(src, 0.0)
            share = (m["spend"] / total) if total > 0 else (1.0 / n)
        else:
            share = 1.0
        revenue = float(src_pb.get("revenue", 0.0)) * share
        pb_clicks = float(src_pb.get("clicks", 0)) * share
        pb_conv = float(src_pb.get("conversions", 0)) * share
        src_clicks = int(src_pb.get("clicks", 0))
        src_conv = int(src_pb.get("conversions", 0))
        rows.append({
            "r": r, "m": m, "account_name": name, "source": src, "blocked": blocked, "health": h, "tab": tab, "err_at": err_at,
            "ag": ({"on": True, "live": ag_live,
                    **{k: (ag_metrics.get(r.campaign_id) or {}).get(k, 0) for k in ("active_n", "total_n", "hidden_spend", "hidden_conversions")},
                    **{k: ag_revenue.get(r.campaign_id, {}).get(k, 0) for k in ("unsplit_revenue", "unsplit_conversions")}}
                   if ag_on else None),
            "shared_n": n if (src and n > 1) else 0,
            "revenue": revenue,
            "pb_clicks": pb_clicks,
            "pb_conversions": pb_conv,
            # CVR is a ratio → identical for every campaign on the source
            "cvr": (src_conv / src_clicks * 100) if src_clicks else 0.0,
            "profit": revenue - m["spend"],
            "roas": (revenue / m["spend"]) if (src and m["spend"]) else 0.0,
            # earnings per TikTok click — what a click actually pays back
            "epc": (revenue / m["clicks"]) if (src and m["clicks"]) else 0.0,
        })

    reverse = sort not in ("name", "source")
    keyfn = SORT_KEYS[sort]
    rows.sort(key=lambda row: keyfn(row) or (0 if reverse else ""), reverse=reverse)
    if state == "pending" and "sort" not in request.query_params:
        # Pending: the one waiting longest first (a spend sort means nothing — none of them spend)
        _far = datetime(2999, 1, 1)
        rows.sort(key=lambda row: _naive((row["health"] or {}).get("since")) or _far)

    # lifetime spend / revenue / ROAS on each row (range-independent)
    life = board_numbers.lifetime(db, models, [row["r"].campaign_id for row in rows], sources)
    # scaling (v155): what earned more (lifetime numbers), and any "×N once approved" still waiting
    from .. import scaling
    from ..settings_store import get_settings as _gs
    scale_th = scaling.thresholds(_gs(db, sc.owner_for_new))
    watches = scaling.open_watches(db, models, [row["r"].campaign_id for row in rows])
    for row in rows:
        row["life"] = life.get(row["r"].campaign_id)
        row["rec"] = scaling.advise(row["life"], row["tab"], row["r"].operation_status, scale_th)
        row["watch"] = watches.get(row["r"].campaign_id)

    # totals across the FILTERED rows; rate metrics recomputed from the sums so
    # they're properly weighted (never an average of averages)
    spend = sum(row["m"]["spend"] for row in rows)
    impressions = sum(row["m"]["impressions"] for row in rows)
    clicks = sum(row["m"]["clicks"] for row in rows)
    conversions = sum(row["m"]["conversions"] for row in rows)
    revenue = sum(row["revenue"] for row in rows)
    pb_clicks = sum(row["pb_clicks"] for row in rows)
    pb_conversions = sum(row["pb_conversions"] for row in rows)
    totals = {
        "spend": spend, "impressions": impressions, "clicks": clicks,
        "conversions": conversions,
        "ctr": (clicks / impressions * 100) if impressions else 0.0,
        "cpc": (spend / clicks) if clicks else 0.0,
        "cpm": (spend / impressions * 1000) if impressions else 0.0,
        "cpa": (spend / conversions) if conversions else 0.0,
        "revenue": revenue,
        "pb_clicks": pb_clicks,
        "cvr": (pb_conversions / pb_clicks * 100) if pb_clicks else 0.0,
        "profit": revenue - spend,
        "roas": (revenue / spend) if spend else 0.0,
        "epc": (revenue / clicks) if clicks else 0.0,
    }
    active = sum(1 for row in rows if row["r"].operation_status == "ENABLE")
    # how many rows each Status view would show (the segmented control's counts)
    state_counts = {"active": 0, "pending": 0, "blocked": 0, "paused": 0, "all": 0, "blocked_older": 0}
    _range_start = start_utc.replace(tzinfo=None)
    for r in records:
        if not sc.allows(r.advertiser_id):
            continue
        acct_ = accounts.get(r.advertiser_id)
        if account and r.advertiser_id != account:
            continue
        if source_f and sources.get(r.campaign_id, "") != source_f:
            continue
        nm = ((acct_.advertiser_name if acct_ else r.advertiser_id) or r.advertiser_id).lower()
        if q and q not in (r.campaign_name or "").lower() and q not in nm:
            continue
        if not board_filter(r, **_bf):
            continue
        state_counts["all"] += 1
        tab_, _blk = bucket_of(r, acct_, healths.get(r.campaign_id))
        if tab_ == "blocked" and not older:
            ea = board_numbers.error_at(healths.get(r.campaign_id), acct_, _acct_level(_blk), r.campaign_id, issue_at)
            if ea is not None and ea < _range_start:
                state_counts["blocked_older"] += 1
                continue
        if tab_ in state_counts:
            state_counts[tab_] += 1

    # account dropdown: only accounts that actually have campaigns cached
    adv_ids_with_campaigns = {r.advertiser_id for r in records if sc.allows(r.advertiser_id)}
    in_view_recs = [r for r in records if sc.allows(r.advertiser_id)]
    hidden_n = sum(1 for r in in_view_recs if r.campaign_id in hidden_set)
    objective_options = sorted({(r.objective_type or "") for r in in_view_recs if r.objective_type}, key=objective_label)
    no_offer_n = sum(1 for r in in_view_recs if not sources.get(r.campaign_id) and r.campaign_id not in hidden_set)
    # team leaderboard (owner, on "Everyone"): the board's own rows by buyer — same range, same filters
    team = []
    if sc.everything and guard.is_owner(request):
        _owner_by_adv = {aid: a.owner_user_id for aid, a in accounts.items()}
        _unames = {u.id: (u.email or "").split("@")[0] for u in db.query(models.User).all()}
        team = leaderboard(rows, _owner_by_adv, _unames)
    account_options = sorted(
        ((aid, (accounts[aid].advertiser_name or aid) if aid in accounts else aid)
         for aid in adv_ids_with_campaigns),
        key=lambda t: t[1].lower())

    # --- entity linking: creative / spark / BC per campaign ---------------------
    shown_ids = [row["r"].campaign_id for row in rows]
    creative_by_cid: dict[str, models.Creative] = {}
    if shown_ids:
        for c in (db.query(models.Creative)
                  .filter(models.Creative.used_campaign_id.in_(shown_ids))):
            creative_by_cid.setdefault(c.used_campaign_id, c)
    spark_by_cid: dict[str, str] = {}
    if shown_ids:
        spark_names = {s.id: (s.name or "") for s in db.query(models.SparkCode).all()}
        for log_row in (db.query(models.LaunchLog)
                        .filter(models.LaunchLog.campaign_id.in_(shown_ids),
                                models.LaunchLog.spark_code_id != None)):        # noqa: E711
            spark_by_cid.setdefault(log_row.campaign_id, spark_names.get(log_row.spark_code_id, ""))
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    bc_by_aid = {aid: bc_names.get(a.owner_bc_id, "") for aid, a in accounts.items() if a.owner_bc_id}
    source_options = sorted({s for s in sources.values() if s})

    # --- KPI period-over-period deltas + sparklines (DB-only, no API cost) -----
    # Previous window = equal-length span immediately before the current one.
    # Basis is the SAME campaign/source set as the visible rows so the delta is
    # consistent with each tile's headline; only shown when a prior figure exists.
    import json as _json2

    from sqlalchemy import func as _func
    shown_cids = [row["r"].campaign_id for row in rows]
    shown_srcs = {row["source"] for row in rows if row["source"]}
    span = end_utc - start_utc
    prev_start, prev_end = start_utc - span, start_utc
    prev_spend = 0.0
    if shown_cids:
        ps_day = timeutil.local_date_str(prev_start)
        pe_day = timeutil.local_date_str(prev_end - _td(seconds=1))
        prev_spend = float(
            db.query(_func.coalesce(_func.sum(models.SpendSnapshot.spend), 0.0))
            .filter(models.SpendSnapshot.campaign_id.in_(shown_cids),
                    models.SpendSnapshot.day >= ps_day,
                    models.SpendSnapshot.day <= pe_day).scalar() or 0)
    prev_pb = pnl_data.revenue_by_source(db, prev_start, prev_end, sc.ids)
    prev_rev = sum(prev_pb.get(s, {}).get("revenue", 0.0) for s in shown_srcs)
    prev = {"spend": prev_spend, "revenue": prev_rev,
            "profit": prev_rev - prev_spend,
            "roas": (prev_rev / prev_spend) if prev_spend else 0.0}

    def _pct(cur, was):
        return ((cur - was) / was * 100) if was else None
    cur_roas = (totals["revenue"] / totals["spend"]) if totals["spend"] else 0.0
    deltas = {
        "has_prev": bool(prev_spend > 0 or prev_rev > 0),
        "spend_pct": _pct(totals["spend"], prev["spend"]),
        "revenue_pct": _pct(totals["revenue"], prev["revenue"]),
        "profit_abs": totals["profit"] - prev["profit"],   # $ change (signed base)
        "roas_abs": (cur_roas - prev["roas"]) if prev["spend"] else None,
    }

    # sparklines: real per-day series, only when the range is wide enough to read
    spark = {}
    days_in_range = round(span.total_seconds() / 86400)
    if days_in_range >= 3 and shown_cids:
        _days, _sp, _rv = pnl_data.daily_series(
            db, start_utc, end_utc, shown_cids, shown_srcs)
        spark = {"spend": _sp, "revenue": _rv}

    # delivery pace (today only — history ticks are cumulative today-values)
    pace_by_cid: dict = {}
    if range_key == "today" and rows:
        from .. import pace as _pace
        pace_by_cid = _pace.compute(db, {row["r"].campaign_id: row["m"] for row in rows
                                         if row["r"].operation_status == "ENABLE"})
    pace_tot = {"impressions": 0, "clicks": 0, "spend": 0.0, "n": 0}
    for p in pace_by_cid.values():
        w = p["w"].get(15)
        if w and w["impressions"] is not None:
            pace_tot["impressions"] += w["impressions"]; pace_tot["clicks"] += w["clicks"]
            pace_tot["spend"] += w["spend"]; pace_tot["n"] += 1

    # --- per-row 'last 12h' spend bars (today only; from the hourly rollups) ----
    trend: dict[str, list[float]] = {}
    if range_key == "today" and shown_ids:
        from .. import hourly as _hourly
        h_now = timeutil.now_local().hour
        for cid, vals in _hourly.series_by_campaign(db, shown_ids, timeutil.local_date_str(start_utc)).items():
            lo = max(0, h_now - 11)
            trend[cid] = vals[lo:h_now + 1]

    # --- group-by roll-ups (creative / account / BC / source) -------------------
    from .. import activity as _activity
    notes = _activity.notes_for(db, "campaign", shown_ids)
    grouped: list[dict] = []
    family_root: dict[str, models.Creative] = {}
    family_size: dict[str, int] = {}
    if group == "creative" and creative_by_cid:
        fams = {(c.source_md5 or c.md5 or f"c{c.id}") for c in creative_by_cid.values()}
        for c in db.query(models.Creative).filter((models.Creative.source_md5.in_(list(fams))) | (models.Creative.md5.in_(list(fams)))):
            fam = c.source_md5 or c.md5 or f"c{c.id}"
            family_size[fam] = family_size.get(fam, 0) + 1
            if fam not in family_root or (c.md5 == fam) or (family_root[fam].md5 != fam and c.id < family_root[fam].id):
                family_root[fam] = c
    if group:
        buckets: dict[str, dict] = {}
        for row in rows:
            r = row["r"]
            if group == "creative":
                # variants of one upload share source_md5 → one roll-up per ORIGINAL creative
                cr_ = creative_by_cid.get(r.campaign_id)
                sp_ = spark_by_cid.get(r.campaign_id, "")
                if cr_:
                    fam = cr_.source_md5 or cr_.md5 or f"c{cr_.id}"
                    root = family_root.get(fam, cr_)
                    key, label, sub = f"f:{fam}", root.name, ("library creative" + (f" · {family_size.get(fam, 1)} variants" if family_size.get(fam, 1) > 1 else ""))
                else:
                    key, label, sub = ((f"s:{sp_}", sp_, "spark code") if sp_ else ("-", "No creative linked", ""))
            elif group == "account":
                key, label, sub = r.advertiser_id, row["account_name"], bc_by_aid.get(r.advertiser_id, "")
            elif group == "bc":
                b = bc_by_aid.get(r.advertiser_id, "")
                key, label, sub = (b or "-"), (b or "No Business Center"), ""
            else:
                key, label, sub = (row["source"] or "-"), (row["source"] or "No source"), ""
            g = buckets.get(key)
            if g is None:
                g = buckets[key] = {"key": key, "label": label, "sub": sub, "rows": [], "creative": (family_root.get(cr_.source_md5 or cr_.md5 or "", cr_) if (group == "creative" and cr_) else None),
                                    "spend": 0.0, "revenue": 0.0, "profit": 0.0, "conversions": 0, "clicks": 0, "impressions": 0, "active": 0, "has_rev": False}
            g["rows"].append(row)
            g["spend"] += row["m"]["spend"]; g["revenue"] += row["revenue"]; g["profit"] += row["profit"]
            g["conversions"] += row["m"]["conversions"]; g["clicks"] += row["m"]["clicks"]; g["impressions"] += row["m"]["impressions"]
            g["active"] += 1 if r.operation_status == "ENABLE" else 0
            g["has_rev"] = g["has_rev"] or bool(row["source"])
        for g in buckets.values():
            g["n"] = len(g["rows"])
            g["roas"] = (g["revenue"] / g["spend"]) if g["spend"] else 0.0
            g["epc"] = (g["revenue"] / g["clicks"]) if g["clicks"] else 0.0
            g["cpa"] = (g["spend"] / g["conversions"]) if g["conversions"] else 0.0
        gkey = {"spend": lambda g: g["spend"], "revenue": lambda g: g["revenue"], "profit": lambda g: g["profit"], "roas": lambda g: g["roas"],
                "epc": lambda g: g["epc"], "conv": lambda g: g["conversions"], "name": lambda g: g["label"].lower()}.get(sort, lambda g: g["spend"])
        grouped = sorted(buckets.values(), key=gkey, reverse=sort != "name")

    # --- reconciliation: what the totals above leave out, so they add up -------------
    recon = {"unmatched": 0.0, "unmatched_items": [], "outside": 0.0, "outside_items": []}
    if sc.ids is None:
        # revenue on a source no campaign carries (a typo'd source, an old campaign) — company-wide,
        # so only on the everything view
        recon["unmatched"], recon["unmatched_items"] = board_numbers.unmatched(pb, sources.values())
    if origin == "tool":
        _names = {aid: (a.advertiser_name or aid) for aid, a in accounts.items()}
        not_ours = [r for r in all_records if sc.allows(r.advertiser_id) and r.campaign_id not in tool_campaign_ids
                    and (not account or r.advertiser_id == account)]
        recon["outside"], recon["outside_items"] = board_numbers.outside_spend(
            db, models, not_ours, range_key, timeutil.local_date_str(start_utc), timeutil.local_date_str(end_utc - _td(seconds=1)), _names)

    recon["hidden"], recon["hidden_n"] = 0.0, 0
    if hidden_set and not show_hidden:
        # what the campaigns you hid spent in the range — left out of the totals above
        _hid = [r for r in records if r.campaign_id in hidden_set and sc.allows(r.advertiser_id)
                and (not account or r.advertiser_id == account)]
        recon["hidden"], _items = board_numbers.outside_spend(
            db, models, _hid, range_key, timeutil.local_date_str(start_utc), timeutil.local_date_str(end_utc - _td(seconds=1)), {})
        recon["hidden_n"] = len(_hid)

    batch_by_cid = {}
    _shown = [row["r"].campaign_id for row in rows if row.get("rec")]
    if _shown:
        for lg in db.query(models.LaunchLog.campaign_id, models.LaunchLog.batch_ref).filter(models.LaunchLog.campaign_id.in_(_shown)):
            batch_by_cid.setdefault(lg.campaign_id, lg.batch_ref)
    return render(request, "status.html", {
        "recon": recon, "older": older, "scale_th": scale_th, "batch_by_cid": batch_by_cid,
        "rec_count": sum(1 for row in rows if row.get("rec")),
        "show_hidden": show_hidden, "hidden_n": hidden_n, "hidden_set": hidden_set, "obj_f": obj_f, "offer_f": offer_f,
        "nowarm": nowarm, "objective_options": [(o, objective_label(o)) for o in objective_options],
        "no_offer_n": no_offer_n, "team": team,
        "rejections": appeals_mod.by_campaign(db),
        "view": sc,
        "ag_flags": ag_flags,
        "tags_by_cid": tags_mod.by_campaign(db, [row["r"].campaign_id for row in rows], owner_user_id=sc.user_id),
        "tag_colors": tags_mod.COLORS,
        "group": group, "grouped": grouped, "notes": notes, "state_counts": state_counts, "trend": trend,
        "pace": pace_by_cid, "pace_tot": pace_tot,
        "deltas": deltas, "spark_json": _json2.dumps(spark),
        "creative_by_cid": creative_by_cid, "spark_by_cid": spark_by_cid,
        "bc_by_aid": bc_by_aid, "source_f": source_f, "source_options": source_options,
        "rows": rows, "totals": totals, "active_count": active,
        "synced_ago": queries.campaigns_synced_ago(db),
        "q": q, "state": state, "account": account, "sort": sort, "origin": origin,
        "range_key": range_key, "start": start or "", "end": end or "", "range_from_db": range_from_db,
        "range_errors": range_errors,
        "pending_tool": pending_tool, "pending_details": pending_details,
        "account_options": account_options,
        "title": "Campaigns",
    })


@router.post("/status/verify-pending")
def verify_pending(request: Request, db: Session = Depends(get_db)):
    """For every tool-launched campaign missing from the cache, query TikTok BY
    CAMPAIGN ID (including deleted status) and report exactly what it says.
    Only the workspace in view — one buyer's click must not walk everyone's launches."""
    import json as _json

    from .. import tiktok_api, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    cached = {r.campaign_id for r in db.query(models.CampaignRecord.campaign_id)}
    tool_logs = (db.query(models.LaunchLog)
                 .filter(models.LaunchLog.ok == True,          # noqa: E712
                         models.LaunchLog.campaign_id != "").all())
    pending = [l for l in tool_logs if l.campaign_id not in cached and sc.allows(l.advertiser_id)]
    if not pending:
        return RedirectResponse("/status?ok=nothing+pending+to+verify", status_code=303)
    results = []
    for log in pending[:10]:
        acct = (db.query(models.AdAccount)
                .filter_by(advertiser_id=log.advertiser_id).first())
        if not acct or not acct.access_token:
            results.append(f"{log.campaign_id}: account not connected")
            continue
        try:
            data = tiktok_api.list_campaigns(
                acct.access_token, acct.advertiser_id,
                filtering={"campaign_ids": [log.campaign_id]})
            found = data.get("list", [])
            if not found:
                # not in the default listing — is it DELETED?
                data2 = tiktok_api.list_campaigns(
                    acct.access_token, acct.advertiser_id,
                    filtering={"campaign_ids": [log.campaign_id],
                               "secondary_status": "CAMPAIGN_STATUS_DELETE"})
                if data2.get("list", []):
                    results.append(f"{log.campaign_id}: DELETED on TikTok (removed in Ads Manager)")
                    # stop counting it as pending — clear the stale link
                    log.campaign_id = f"deleted:{log.campaign_id}"
                    db.commit()
                else:
                    results.append(f"{log.campaign_id}: NOT FOUND on account {log.advertiser_id}")
            else:
                c = found[0]
                # found but missing from cache → self-heal: insert it now
                if not (db.query(models.CampaignRecord)
                        .filter_by(campaign_id=log.campaign_id).first()):
                    db.add(models.CampaignRecord(
                        advertiser_id=log.advertiser_id,
                        campaign_id=log.campaign_id,
                        campaign_name=c.get("campaign_name", ""),
                        objective_type=c.get("objective_type", ""),
                        operation_status=c.get("operation_status", ""),
                        secondary_status=c.get("secondary_status", ""),
                        budget=float(c.get("budget", 0) or 0),
                        budget_mode=c.get("budget_mode", "")))
                    db.commit()
                results.append(
                    f"{log.campaign_id}: EXISTS ({c.get('operation_status')}, "
                    f"{c.get('secondary_status', '?')}) — added to the list")
        except tiktok_api.TikTokError as e:
            results.append(f"{log.campaign_id}: lookup failed, code {e.code} {e.message[:60]}")
    msg = " · ".join(results)[:400].replace(" ", "+")
    return RedirectResponse(f"/status?ok={msg}", status_code=303)


@router.post("/status/sync")
def sync_now(request: Request, db: Session = Depends(get_db)):
    """Queue a full campaign sync; the page refreshes itself when the job's
    notification arrives. Returns JSON to fetch() callers, a redirect otherwise."""
    from .. import jobs
    from fastapi.responses import JSONResponse
    running = (db.query(models.Job).filter(models.Job.kind == "status_sync",
                                           models.Job.status.in_(("queued", "claimed", "running"))).count())
    if running:
        job_id = None
    else:
        job_id = jobs.enqueue(db, "status_sync", "Sync campaigns from TikTok", {}, href="/status").id
    if request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"queued": True, "job_id": job_id, "already": bool(running)})
    return RedirectResponse("/status?ok=Syncing+in+the+background+—+you%27ll+get+a+notification.", status_code=303)


@router.get("/campaigns/{advertiser_id}/{campaign_id}/detail")
def campaign_detail(advertiser_id: str, campaign_id: str, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """Everything the campaign drawer shows, as JSON: today's metrics + P&L,
    hourly trend today vs yesterday, timeline, note, creative, links."""
    from datetime import timedelta as _td
    from fastapi.responses import JSONResponse
    from .. import activity, hourly
    rec = (db.query(models.CampaignRecord)
           .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
    if rec is None:
        return JSONResponse({"error": "campaign not in the synced cache yet"}, status_code=404)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    sources = pnl_data.campaign_source_map(db)
    src = sources.get(campaign_id, "")
    # revenue share: same apportioning as the table (spend share among campaigns on the source)
    start_utc, end_utc = timeutil.range_bounds("today")
    pb = pnl_data.revenue_by_source(db, start_utc, end_utc).get(src, {}) if src else {}
    y_start, y_end = timeutil.range_bounds("yesterday")
    pb_y = pnl_data.revenue_by_source(db, y_start, y_end).get(src, {}) if src else {}
    share = 1.0
    if src:
        siblings = [r for r in db.query(models.CampaignRecord).all() if sources.get(r.campaign_id, "") == src]
        total = sum(float(r.spend_today or 0) for r in siblings)
        if len(siblings) > 1:
            share = (float(rec.spend_today or 0) / total) if total > 0 else 1.0 / len(siblings)
    spend = float(rec.spend_today or 0)
    revenue = float(pb.get("revenue", 0.0)) * share
    clicks = int(rec.clicks or 0)
    m = {"spend": spend, "revenue": revenue, "profit": revenue - spend,
         "roas": (revenue / spend) if (src and spend) else 0.0, "epc": (revenue / clicks) if (src and clicks) else 0.0,
         "impressions": int(rec.impressions or 0), "clicks": clicks, "conversions": int(rec.conversions or 0),
         "ctr": float(rec.ctr or 0), "cpc": float(rec.cpc or 0), "cpm": float(rec.cpm or 0), "cpa": float(rec.cpa or 0),
         "pb_clicks": float(pb.get("clicks", 0)) * share, "pb_conversions": float(pb.get("conversions", 0)) * share,
         "share": share, "shared_n": len(siblings) if src else 0}
    # yesterday for the same campaign (snapshot) + its revenue share of yesterday
    y_day = timeutil.local_date_str(y_start)
    snap = db.query(models.SpendSnapshot).filter_by(campaign_id=campaign_id, day=y_day).first()
    y_spend = float(snap.spend) if snap else 0.0
    y_rev = float(pb_y.get("revenue", 0.0)) * share
    today = timeutil.local_date_str(start_utc)
    h_today, h_y = hourly.series(db, [campaign_id], today), hourly.series(db, [campaign_id], y_day)
    rev_today, rev_y = hourly.revenue_series(db, {src} if src else set(), today), hourly.revenue_series(db, {src} if src else set(), y_day)
    if src and m["shared_n"] > 1:
        rev_today = [v * share for v in rev_today]; rev_y = [v * share for v in rev_y]
    cr = db.query(models.Creative).filter(models.Creative.used_campaign_id == campaign_id).first()
    spark_name = ""
    lg = (db.query(models.LaunchLog).filter(models.LaunchLog.campaign_id == campaign_id,
                                             models.LaunchLog.spark_code_id != None).first())    # noqa: E711
    if lg:
        sp = db.get(models.SparkCode, lg.spark_code_id)
        spark_name = sp.name if sp else ""
    bc = db.query(models.BusinessCenter).filter_by(bc_id=acct.owner_bc_id).first() if (acct and acct.owner_bc_id) else None
    note = activity.get_note(db, "campaign", campaign_id)
    tl = [{"at": (i["at"].isoformat() + "Z") if i["at"] else "", "ago": _ago(i["at"]), "action": i["action"], "detail": i["detail"], "who": i["who"]}
          for i in activity.timeline(db, campaign_id)]
    return JSONResponse({
        "campaign": {"id": campaign_id, "name": rec.campaign_name, "advertiser_id": advertiser_id,
                     "account": (acct.advertiser_name if acct else advertiser_id) or advertiser_id,
                     "account_status": (acct.status if acct else "") or "", "bc": (bc.name if bc else ""),
                     "status": rec.operation_status, "secondary": (rec.secondary_status or "").replace("CAMPAIGN_STATUS_", "").replace("_", " ").lower(),
                     "blocked": blocked_reason(rec, acct) if rec.operation_status == "ENABLE" else "",
                     "health": _health_json(rec, acct, db),
                     "budget": float(rec.budget or 0), "budget_mode": rec.budget_mode or "", "objective": rec.objective_type or "",
                     "smart_plus": bool(rec.is_smart_plus), "launched_at": rec.launched_at.isoformat() + "Z" if rec.launched_at else "",
                     "launched_ago": _ago(rec.launched_at), "source": src,
                     "ads_manager_url": f"https://ads.tiktok.com/i18n/dashboard?aadvid={advertiser_id}"},
        "metrics": m,
        "yesterday": {"spend": y_spend, "revenue": y_rev, "profit": y_rev - y_spend, "roas": (y_rev / y_spend) if y_spend else 0.0},
        "hourly": {"today": {"spend": h_today["spend"], "conversions": h_today["conversions"], "clicks": h_today["clicks"], "revenue": rev_today},
                   "yesterday": {"spend": h_y["spend"], "conversions": h_y["conversions"], "clicks": h_y["clicks"], "revenue": rev_y},
                   "hour_now": timeutil.now_local().hour},
        "creative": ({"id": cr.id, "name": cr.name, "kind": cr.kind or "video", "file": f"/creatives/{cr.id}/file", "thumb": f"/creatives/{cr.id}/thumb"} if cr else None),
        "spark": spark_name,
        "note": (note.text if note else ""),
        "timeline": tl,
    })


@router.get("/campaigns/{advertiser_id}/{campaign_id}/adgroups.json")
def campaign_adgroups(advertiser_id: str, campaign_id: str, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """The campaign's ad groups, for the drawer. Read-only."""
    from fastapi.responses import JSONResponse
    from .. import adgroup_copy, jobs as jobs_mod
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct is None or not acct.access_token:
        return JSONResponse({"ok": False, "error": "that ad account is not connected"}, status_code=400)
    try:
        rows = adgroup_copy.list_adgroups(acct, campaign_id)
    except tiktok_api.TikTokError as e:
        return JSONResponse({"ok": False, "error": f"{e.message} (code {e.code})"}, status_code=200)
    job = jobs_mod.pending(db, "adgroup_duplicate")
    # TikTok review rejections per ad group (from the issue scan), with the appeal state
    tracked = appeals_mod.rows_for_campaign(db, advertiser_id, campaign_id)
    # today's numbers per ad group (spend/clicks/conv from the sweep, revenue from postbacks by ad group id)
    today = timeutil.local_date_str()
    s_utc, e_utc = timeutil.range_bounds("today")
    stats = adgroup_stats.drawer_rows(db, campaign_id, today, today)
    src = pnl_data.campaign_source_map(db).get(campaign_id, "")
    rev = adgroup_stats.revenue_by_adgroup(db, src, s_utc.replace(tzinfo=None), e_utc.replace(tzinfo=None))
    for g in rows:
        g["appeal"] = appeals_mod.row_state(tracked.get(g["adgroup_id"]))
        g["rejected_live"] = "deny" in (g.get("secondary_status") or "") or "reject" in (g.get("secondary_status") or "")
        st = stats.get(g["adgroup_id"]) or {}
        rv = rev.get(g["adgroup_id"]) or {}
        g["stats"] = {"spend": float(st.get("spend") or 0), "clicks": int(st.get("clicks") or 0),
                      "conversions": int(st.get("conversions") or 0), "revenue": float(rv.get("revenue") or 0),
                      "pb_conversions": int(rv.get("conversions") or 0), "has": bool(st)}
        g["created"] = (st.get("created") or "")
    unsplit = rev.get("", {})
    return JSONResponse({"ok": True, "adgroups": rows, "max_copies": adgroup_copy.MAX_COPIES,
                         "running": bool(job), "ag_active_only": str(campaign_id) in adgroup_stats.flagged(db),
                         "unsplit": {"revenue": float(unsplit.get("revenue") or 0), "conversions": int(unsplit.get("conversions") or 0)}})


@router.get("/campaigns/{advertiser_id}/{campaign_id}/adgroups/{adgroup_id}/settings.json")
def adgroup_settings(advertiser_id: str, campaign_id: str, adgroup_id: str, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """What TikTok has stored on one ad group (the drawer's Settings popover). One
    read-only /adgroup/get/ call; nothing written."""
    from fastapi.responses import JSONResponse
    from .. import adgroup_copy, adgroup_view
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct is None or not acct.access_token:
        return JSONResponse({"ok": False, "error": "that ad account is not connected"}, status_code=400)
    db.expunge(acct)   # keep the loaded row usable, then hand the DB connection back while
    db.rollback()      # TikTok answers (v155.8: the pool ran dry on slow lookups)
    try:
        g = adgroup_copy._source_adgroup(acct, campaign_id, adgroup_id)
    except tiktok_api.TikTokError as e:
        return JSONResponse({"ok": False, "error": f"{e.message} (code {e.code})"})
    if not g:
        return JSONResponse({"ok": False, "error": "TikTok didn't return that ad group"})
    return JSONResponse({"ok": True, "adgroup_id": str(adgroup_id), "name": g.get("adgroup_name") or "",
                         "rows": adgroup_view.rows(g)})


@router.get("/campaigns/{advertiser_id}/{campaign_id}/settings.json")
def campaign_settings(advertiser_id: str, campaign_id: str, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """What TikTok has stored on the campaign itself (objective, automation, budget mode…).
    One read-only /campaign/get/ call filtered to this id."""
    from fastapi.responses import JSONResponse
    from .. import adgroup_view
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct is None or not acct.access_token:
        return JSONResponse({"ok": False, "error": "that ad account is not connected"}, status_code=400)
    token = acct.access_token
    db.rollback()      # hand the DB connection back while TikTok answers (v155.8)
    try:
        data = tiktok_api.list_campaigns(token, advertiser_id, filtering={"campaign_ids": [str(campaign_id)]}) or {}
    except tiktok_api.TikTokError as e:
        return JSONResponse({"ok": False, "error": f"{e.message} (code {e.code})"})
    c = next((x for x in (data.get("list") or []) if str(x.get("campaign_id")) == str(campaign_id)), None)
    if not c:
        return JSONResponse({"ok": False, "error": "TikTok didn't return that campaign"})
    return JSONResponse({"ok": True, "campaign_id": str(campaign_id), "name": c.get("campaign_name") or "",
                         "rows": adgroup_view.rows(c, campaign=True)})


@router.post("/campaigns/{advertiser_id}/{campaign_id}/agmode")
def campaign_agmode(advertiser_id: str, campaign_id: str, on: str = Form("0"), db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """Drawer toggle: this campaign's numbers from ACTIVE ad groups only (on=1) or all (on=0)."""
    from fastapi.responses import JSONResponse
    want = str(on) in ("1", "true", "on", "yes")
    adgroup_stats.set_flag(db, campaign_id, want)
    db.commit()
    return JSONResponse({"ok": True, "ag_active_only": want})


@router.get("/campaigns/{advertiser_id}/{campaign_id}/funnel.json")
def campaign_funnel(advertiser_id: str, campaign_id: str, request: Request, db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """The Lander funnel row for this campaign's source (the drawer). today | yesterday | 7d | 30d."""
    from fastapi.responses import JSONResponse
    from sqlalchemy import func as _func
    from .. import funnel
    src = pnl_data.campaign_source_map(db).get(campaign_id, "")
    if not src:
        rec = db.query(models.CampaignRecord.campaign_name).filter_by(campaign_id=campaign_id).first()
        src = (rec[0] or "") if rec else ""
    if not src:
        return JSONResponse({"ok": True, "row": None, "source": ""})
    range_key = request.query_params.get("range", "today")
    if range_key not in ("today", "yesterday", "7d", "30d"):
        range_key = "today"
    start_utc, end_utc = timeutil.range_bounds(range_key)
    s_naive, e_naive = start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)
    conv = {}
    got = (db.query(_func.sum(models.PostbackEvent.revenue), _func.sum(models.PostbackEvent.conversions))
             .filter(models.PostbackEvent.source == src, models.PostbackEvent.created_at >= s_naive,
                     models.PostbackEvent.created_at < e_naive).first())
    if got:
        conv[src] = {"revenue": float(got[0] or 0), "conversions": int(got[1] or 0)}
    rows = funnel.rows(db, s_naive, e_naive, conv, source=src)
    return JSONResponse({"ok": True, "row": rows[0] if rows else None, "source": src, "range": range_key})


@router.post("/campaigns/{advertiser_id}/{campaign_id}/adgroups/{adgroup_id}/appeal")
def campaign_adgroup_appeal(advertiser_id: str, campaign_id: str, adgroup_id: str,
                            db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """One-click appeal from the campaign drawer. Uses the rejection the issue scan
    tracked (or reads it from TikTok right now if the scan hasn't been by), then files
    through the same job the Appeals page uses — one appeal per rejection, never twice."""
    from fastapi.responses import JSONResponse
    from .. import jobs as jobs_mod
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct is None or not acct.access_token:
        return JSONResponse({"ok": False, "error": "that ad account is not connected"})
    row = appeals_mod.rows_for_campaign(db, advertiser_id, campaign_id).get(str(adgroup_id))
    if row is None:
        try:
            row = appeals_mod.track_adgroup_now(db, acct, campaign_id, adgroup_id)
        except tiktok_api.TikTokError as e:
            return JSONResponse({"ok": False, "error": f"couldn't read the ad group's review state: {e.message} (code {e.code})"})
    if row is None:
        return JSONResponse({"ok": False, "error": "TikTok reports nothing appealable on this ad group right now"})
    if row.status == "appealing":
        return JSONResponse({"ok": False, "error": "an appeal is already on file for this ad group — TikTok allows one per rejection", "appeal": appeals_mod.row_state(row)})
    if row.status in ("successful", "done", "failed", "dismissed"):
        return JSONResponse({"ok": False, "error": f"this rejection was already handled ({appeals_mod.STATUS_LABELS.get(row.status, row.status)}) — TikTok allows one appeal per rejection", "appeal": appeals_mod.row_state(row)})
    jobs_mod.enqueue(db, "appeals_file", f"Appeal “{(row.ad_name or row.adgroup_id)[:50]}”",
                     {"ids": [row.id], "reason": ""}, href="/appeals")
    return JSONResponse({"ok": True, "appeal": appeals_mod.row_state(row)})


@router.get("/appeals/{row_id}/state.json")
def appeal_state(row_id: int, db: Session = Depends(get_db)):
    """Cheap poll for the drawer while the appeal job runs."""
    from fastapi.responses import JSONResponse
    row = db.get(models.Appeal, row_id)
    return JSONResponse({"ok": row is not None, "appeal": appeals_mod.row_state(row)})


@router.post("/campaigns/{advertiser_id}/{campaign_id}/scale")
def campaign_scale(request: Request, advertiser_id: str, campaign_id: str, copies: int = Form(0), when: str = Form("now"),
                   db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """Scale ×N (v155): copy the campaign's best delivering ad group now, or once TikTok approves
    one (`when=approved`). Never automatic — this is the operator's click."""
    from fastapi.responses import JSONResponse
    from .. import adgroup_copy, scaling
    from ..settings_store import get_settings as _gs
    sc = scope_mod.for_request(request, db)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct is None or not acct.access_token:
        return JSONResponse({"ok": False, "error": "that ad account is not connected"})
    n = copies or scaling.thresholds(_gs(db, sc.owner_for_new))["copies"]
    n = max(1, min(int(n), adgroup_copy.MAX_COPIES))
    if when == "approved":
        src = scaling.pick_source(db, models, campaign_id, delivering_only=False)
        if src is None:
            return JSONResponse({"ok": False, "error": "no ad group of this campaign is known yet — wait for the next sync"})
        w = scaling.watch(db, models, sc.owner_for_new, advertiser_id, campaign_id, src.adgroup_id, n)
        fired = scaling.tick(db, models) if "DELIVERY_OK" in (src.secondary_status or "").upper() else 0
        return JSONResponse({"ok": True, "watch_id": w.id, "msg": (f"It's already delivering — {n} copies queued." if fired else
                                                                 f"Waiting for TikTok's approval — then {n} copies are made. Cancel it on the row any time.")})
    src = scaling.pick_source(db, models, campaign_id, delivering_only=True)
    if src is None:
        return JSONResponse({"ok": False, "error": "nothing in this campaign is delivering yet — use “once approved” instead"})
    ok, msg = scaling.queue_copies(db, advertiser_id, campaign_id, src.adgroup_id, n)
    return JSONResponse({"ok": ok, "msg": msg, "error": "" if ok else msg})


@router.post("/campaigns/hide")
def campaigns_hide(request: Request, cids: str = Form(""), hide: str = Form("1"), db: Session = Depends(get_db)):
    """Hide campaigns from YOUR board (v155) — nothing changes on TikTok; "Hidden (n)" shows them."""
    import json as _j
    from fastapi.responses import JSONResponse
    sc = scope_mod.for_request(request, db)
    want = [c.strip() for c in cids.split(",") if c.strip().isdigit()][:500]
    ok_ids = {c for (c, a) in db.query(models.CampaignRecord.campaign_id, models.CampaignRecord.advertiser_id)
              .filter(models.CampaignRecord.campaign_id.in_(want)) if sc.allows(a)} if want else set()
    cur = hidden_campaigns(db, sc)
    cur = (cur | ok_ids) if hide == "1" else (cur - ok_ids)
    queries.set_setting(db, _hidden_key(sc), _j.dumps(sorted(cur)[-5000:]))
    return JSONResponse({"ok": True, "n": len(ok_ids), "hidden": len(cur)})


@router.post("/scale/watch/{watch_id}/cancel")
def scale_watch_cancel(request: Request, watch_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    sc = scope_mod.for_request(request, db)
    w = db.get(models.ScaleWatch, watch_id)
    if w is None or not sc.allows(w.advertiser_id):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if w.status == "waiting":
        w.status, w.detail = "cancelled", "cancelled from the Campaigns page"
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/campaigns/{advertiser_id}/{campaign_id}/adgroups/{adgroup_id}/duplicate")
def campaign_adgroup_duplicate(advertiser_id: str, campaign_id: str, adgroup_id: str,
                               copies: int = Form(1), db: Session = Depends(get_db), _view: scope_mod.Scope = Depends(guard.account_in_view)):
    """Queue N duplicates of one ad group, each with the source's ads, in this campaign."""
    from fastapi.responses import JSONResponse
    from .. import adgroup_copy, jobs as jobs_mod
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if acct is None or not acct.access_token:
        return JSONResponse({"ok": False, "error": "that ad account is not connected"})
    n = max(1, min(int(copies or 1), adgroup_copy.MAX_COPIES))
    job, created = jobs_mod.enqueue_once(
        db, "adgroup_duplicate", f"Duplicate ad group ×{n}",
        {"advertiser_id": advertiser_id, "campaign_id": campaign_id,
         "adgroup_id": adgroup_id, "copies": n}, href="/status")
    if not created:
        return JSONResponse({"ok": False, "error": f"a duplicate run is already {job.status}"})
    return JSONResponse({"ok": True, "queued": n,
                         "msg": f"Making {n} cop{'y' if n == 1 else 'ies'} — the drawer updates when it finishes."})


def _ago(dt):
    from ..templating import _ago as _f
    return _f(dt)


# ---------------------------------------------------------------------------
# campaign tags (Campaigns page pop-up)
# ---------------------------------------------------------------------------

@router.get("/tags.json")
def tags_list(db: Session = Depends(get_db), sc: scope_mod.Scope = Depends(guard.view)):
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": True, "tags": tags_mod.all_tags(db, sc.user_id), "colors": list(tags_mod.COLORS)})


@router.post("/tags")
def tags_create(name: str = Form(""), color: str = Form("grey"), db: Session = Depends(get_db), sc: scope_mod.Scope = Depends(guard.view)):
    from fastapi.responses import JSONResponse
    t, err = tags_mod.create(db, name, color, owner_user_id=sc.owner_for_new)
    if err:
        return JSONResponse({"ok": False, "error": err})
    db.commit()
    return JSONResponse({"ok": True, "tag": tags_mod.as_dict(t)})


@router.post("/tags/{tag_id}")
def tags_update(tag_id: int, name: str = Form(None), color: str = Form(None), db: Session = Depends(get_db), sc: scope_mod.Scope = Depends(guard.view)):
    from fastapi.responses import JSONResponse
    t, err = tags_mod.update(db, tag_id, name=name, color=color, owner_user_id=sc.user_id)
    if err:
        return JSONResponse({"ok": False, "error": err})
    db.commit()
    return JSONResponse({"ok": True, "tag": tags_mod.as_dict(t)})


@router.post("/tags/{tag_id}/delete")
def tags_delete(tag_id: int, db: Session = Depends(get_db), sc: scope_mod.Scope = Depends(guard.view)):
    from fastapi.responses import JSONResponse
    n = tags_mod.delete(db, tag_id, owner_user_id=sc.user_id)
    db.commit()
    return JSONResponse({"ok": True, "removed_from": n})


@router.post("/campaigns/tags")
async def campaigns_tag(request: Request, db: Session = Depends(get_db)):
    """Put one tag on / take it off one or many campaigns: form fields
    campaign_ids (repeated or comma-joined), tag_id, on (1|0)."""
    from fastapi.responses import JSONResponse
    form = await request.form()
    ids: list[str] = []
    for v in form.getlist("campaign_ids"):
        ids.extend(x.strip() for x in str(v).split(",") if x.strip())
    try:
        tag_id = int(form.get("tag_id") or 0)
    except (TypeError, ValueError):
        tag_id = 0
    on = str(form.get("on") or "1") in ("1", "true", "on", "yes")
    sc = scope_mod.for_request(request, db)
    if not sc.everything:
        # only campaigns on the view's accounts can be tagged from this view
        allowed = {r[0] for r in db.query(models.CampaignRecord.campaign_id)
                   .filter(models.CampaignRecord.campaign_id.in_(ids), models.CampaignRecord.advertiser_id.in_(list(sc.ids or [""])))}
        ids = [c for c in ids if c in allowed]
    changed, err = tags_mod.set_on(db, ids, tag_id, on, owner_user_id=sc.user_id)
    if err:
        return JSONResponse({"ok": False, "error": err})
    db.commit()
    # `also`: campaigns whose rows must be repainted too (a group row's other members)
    also = [x.strip() for x in str(form.get("also") or "").split(",") if x.strip()][:2000]
    return JSONResponse({"ok": True, "changed": changed, "tags": tags_mod.by_campaign(db, list(set(ids) | set(also)), owner_user_id=sc.user_id)})

