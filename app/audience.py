"""Audience breakdowns — who the ads actually reach.

Pulled from TikTok's reporting API once a day per account and stored per
campaign per day (AudienceStat), so the Audience page reads from the store
and never waits on TikTok:

  audience report (report_type=AUDIENCE, 10–12 h latency):
      age × gender, country, state/province, OS (platform), placement,
      network (ac), language, device brand
  basic report by hour (stat_time_hour, one day per request):
      the hour-of-day × weekday heatmap

Rules verified against the API guide ("Audience reports → Supported
dimensions / metrics" and "Run a synchronous report"):
  · one audience dimension per query (+ optional ID dim + optional time dim);
    age and gender are the only pair allowed together
  · device_brand_id cannot be combined with a time dimension
  · stat_time_hour limits the date range to ONE day
  · reach / frequency exist only for age, gender, country, province, language
  · page_size max 1,000; rows are {"dimensions": {...}, "metrics": {...}}
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, queries, tiktok_api, timeutil

log = logging.getLogger("adops.audience")

BASE_METRICS = ["spend", "impressions", "clicks", "conversion"]

# dim → (audience dimensions, reach supported)
DIMS: dict[str, dict] = {
    "age_gender":   {"dims": ["age", "gender"], "reach": True},
    "country":      {"dims": ["country_code"], "reach": True},
    "province":     {"dims": ["province_id"], "reach": True},
    "platform":     {"dims": ["platform"], "reach": False},
    "placement":    {"dims": ["placement"], "reach": False},
    "ac":           {"dims": ["ac"], "reach": False},
    "language":     {"dims": ["language"], "reach": True},
    "device_brand": {"dims": ["device_brand_id"], "reach": False, "label_metric": "device_brand_name"},
}
HOUR = "hour"
ALL_DIMS = list(DIMS) + [HOUR]
KEEP_DAYS = 45              # stored history
REGION_REFRESH_DAYS = 7
CALL_GAP = 0.12             # seconds between TikTok calls — rate-limit courtesy at 286 accounts
CHECKPOINT_KEY = "audience_sync_checkpoint"   # {"run": <days key>, "done": [advertiser ids]} while a run is in flight


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rss_mb() -> float:
    """Resident memory in MB (Linux), for the job's own peak-memory breadcrumb."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0


def _f(m: dict, k: str) -> float:
    try:
        v = m.get(k)
        return float(v) if v not in (None, "", "-") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _i(m: dict, k: str) -> int:
    return int(round(_f(m, k)))


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def default_days(today: date | None = None) -> dict[str, list[str]]:
    """What a daily run refreshes: today's hours (near real-time), and the last
    two full days for everything (audience data lags 10–12 h, so day-2 is
    re-pulled once more to pick up late rows)."""
    t = today or date.fromisoformat(timeutil.local_date_str())
    d1, d2 = (t - timedelta(days=1)).isoformat(), (t - timedelta(days=2)).isoformat()
    return {"hours": [t.isoformat(), d1, d2], "audience": [d1, d2]}


STORE_BATCH = 500           # rows per INSERT — the peak memory of a store is ONE batch of plain dicts


class _Sink:
    """Streams one report into audience_stats without ever holding the whole report.

    Memory (v131 → v132): the report is consumed PAGE BY PAGE (get_report_pages'
    on_page), each page written as plain dicts through executemany INSERTs of
    STORE_BATCH — never one ORM object per row, and never the whole report in RAM.
    Old paths held all of an account-day's rows (nine reports × up to 20k rows) as
    pending ORM objects until one commit — a ~300 MB burst that OOM-killed the 512 MB
    instance every sweep. Peak is now one page + one batch.

    Locks: one commit at the end of the report, so the single SQLite writer is
    released before the next report's network call."""
    def __init__(self, db, acct, day, dim, cfg=None):
        from sqlalchemy import insert
        self.db, self.day, self.dim, self.cfg = db, day, dim, cfg
        self.adv = acct.advertiser_id
        self.now = _now()
        self.stmt = insert(models.AudienceStat)
        self.seen: set[tuple[str, str]] = set()
        self.batch: list[dict] = []
        self.n = 0
        db.query(models.AudienceStat).filter_by(advertiser_id=self.adv, date=day, dim=dim) \
            .delete(synchronize_session=False)

    def add(self, rows) -> None:
        for r in rows:
            d = r.get("dimensions") or {}
            m = r.get("metrics") or {}
            cid = str(d.get("campaign_id") or "")
            if not cid:
                continue
            if self.dim == HOUR:
                stamp = str(d.get("stat_time_hour") or "")      # "2026-09-06 14:00:00"
                try:
                    key = str(int(stamp[11:13]))
                except ValueError:
                    continue
                label = ""
            else:
                parts = [str(d.get(x) if d.get(x) is not None else "") for x in self.cfg["dims"]]
                key = "|".join(parts)
                label = str(m.get(self.cfg.get("label_metric", ""), "") or "") if self.cfg.get("label_metric") else ""
            if (cid, key) in self.seen:
                continue
            self.seen.add((cid, key))
            self.batch.append({
                "advertiser_id": self.adv, "campaign_id": cid, "campaign_name": str(m.get("campaign_name") or "")[:200],
                "date": self.day, "dim": self.dim, "key": key[:120], "label": label[:120],
                "spend": _f(m, "spend"), "impressions": _i(m, "impressions"), "clicks": _i(m, "clicks"),
                "conversions": _i(m, "conversion"), "reach": _i(m, "reach"), "synced_at": self.now})
            self.n += 1
            if len(self.batch) >= STORE_BATCH:
                self.db.execute(self.stmt, self.batch)
                self.batch = []

    def finish(self) -> int:
        if self.batch:
            self.db.execute(self.stmt, self.batch)
            self.batch = []
        self.db.commit()
        return self.n


def _store(db: Session, acct: models.AdAccount, day: str, dim: str, rows: list[dict],
           cfg: dict | None = None) -> int:
    """Replace this account's rows for (day, dim) — for a report already in hand."""
    sink = _Sink(db, acct, day, dim, cfg)
    sink.add(rows)
    return sink.finish()


def sync_account_day(db: Session, acct: models.AdAccount, day: str, *, hours: bool, audience: bool,
                     should_stop=None) -> dict:
    """One account, one day. Returns {rows, calls, skipped, error}."""
    out = {"rows": 0, "calls": 0, "skipped": False, "error": ""}
    tok = acct.access_token
    try:
        if hours:
            sink = _Sink(db, acct, day, HOUR)
            tiktok_api.get_hourly_report(tok, acct.advertiser_id, metrics=BASE_METRICS + ["campaign_name"], day=day, on_page=sink.add)
            out["calls"] += 1
            out["rows"] += sink.finish()
            time.sleep(CALL_GAP)
        if audience:
            # age × gender first: an empty answer means no delivery that day →
            # the other seven queries would be empty too, so they are skipped.
            cfg = DIMS["age_gender"]
            sink = _Sink(db, acct, day, "age_gender", cfg)
            tiktok_api.get_audience_report(
                tok, acct.advertiser_id, dimensions=cfg["dims"] + ["campaign_id"],
                metrics=BASE_METRICS + ["reach", "campaign_name"], start_date=day, end_date=day, on_page=sink.add)
            out["calls"] += 1
            stored = sink.finish()
            out["rows"] += stored
            if not stored:
                out["skipped"] = True
                return out
            for dim, cfg in DIMS.items():
                if dim == "age_gender":
                    continue
                if should_stop and should_stop():
                    break
                time.sleep(CALL_GAP)
                metrics = BASE_METRICS + ["campaign_name"] + (["reach"] if cfg["reach"] else []) \
                    + ([cfg["label_metric"]] if cfg.get("label_metric") else [])
                sink = _Sink(db, acct, day, dim, cfg)
                tiktok_api.get_audience_report(
                    tok, acct.advertiser_id, dimensions=cfg["dims"] + ["campaign_id"],
                    metrics=metrics, start_date=day, end_date=day, on_page=sink.add)
                out["calls"] += 1
                out["rows"] += sink.finish()
        db.commit()
    except tiktok_api.TikTokError as e:
        db.rollback()
        out["error"] = str(e)[:200]
    return out


def hot_accounts(db: Session) -> list[models.AdAccount]:
    """Enabled accounts with a token that have at least one active campaign —
    the ones whose numbers can still move today."""
    active = {r[0] for r in (db.query(models.CampaignRecord.advertiser_id)
                             .filter(models.CampaignRecord.operation_status == "ENABLE").distinct().all())}
    return [a for a in queries.enabled_accounts(db) if a.access_token and a.advertiser_id in active]


def sync(db: Session, days: dict[str, list[str]] | None = None, should_stop=None, on_progress=None,
         hot_only: bool = False) -> dict:
    """The refresh job: every enabled account with a token (or, hot_only, just
    the accounts with active campaigns) for the given days. Returns a summary
    for the job notification."""
    days = days or default_days()
    accounts = hot_accounts(db) if hot_only else [a for a in queries.enabled_accounts(db) if a.access_token]
    hour_days, aud_days = list(days.get("hours") or []), list(days.get("audience") or [])
    all_days = sorted(set(hour_days) | set(aud_days), reverse=True)
    stats = {"accounts": len(accounts), "ok": 0, "failed": 0, "rows": 0, "calls": 0, "stopped": False,
             "errors": [], "resumed": 0}
    # Checkpoint (v131): the run over 100+ accounts takes long; if the process is
    # restarted mid-way (deploy, out-of-memory), the next run of the SAME days
    # resumes after the accounts already done instead of starting from the first
    # account again — which is how one run kept restarting from scratch forever.
    run_key = json.dumps({"hours": sorted(hour_days), "audience": sorted(aud_days), "hot": bool(hot_only)}, sort_keys=True)
    done: set[str] = set()
    try:
        ck = json.loads(queries.get_setting(db, CHECKPOINT_KEY, "") or "{}")
        if ck.get("run") == run_key:
            done = set(str(x) for x in (ck.get("done") or []))
    except (ValueError, TypeError):
        done = set()
    for i, acct in enumerate(accounts, 1):
        if should_stop and should_stop():
            stats["stopped"] = True
            break
        if acct.advertiser_id in done:
            stats["resumed"] += 1
            stats["ok"] += 1
            continue
        if on_progress and (i == 1 or i % 5 == 0 or i == len(accounts)):
            on_progress(f"{i} of {len(accounts)} accounts")
        try:
            from . import background
            background.set_activity(f"audience {i}/{len(accounts)} {acct.advertiser_name or acct.advertiser_id}")
        except Exception:  # noqa: BLE001
            pass
        failed = False
        for day in all_days:
            r = sync_account_day(db, acct, day, hours=day in hour_days, audience=day in aud_days,
                                 should_stop=should_stop)
            stats["rows"] += r["rows"]
            stats["calls"] += r["calls"]
            if r["error"]:
                failed = True
                if len(stats["errors"]) < 30:
                    stats["errors"].append({"advertiser_id": acct.advertiser_id,
                                            "name": acct.advertiser_name or acct.advertiser_id, "error": r["error"]})
                break
        stats["failed" if failed else "ok"] += 1
        mb = _rss_mb()
        if mb > stats.get("peak_mb", 0):
            stats["peak_mb"], stats["peak_acct"] = mb, (acct.advertiser_name or acct.advertiser_id)
        done.add(acct.advertiser_id)
        queries.set_setting(db, CHECKPOINT_KEY, json.dumps({"run": run_key, "done": sorted(done)}))
    if stats.get("peak_mb", 0) >= 360:      # this job runs in the jobs worker, not the sweep — it records its own peak
        try:
            queries.log(db, f"audience sync memory peaked at {stats['peak_mb']:.0f} MB (account {stats.get('peak_acct','?')}, limit 512)", level="warning", source="mem")
        except Exception:  # noqa: BLE001
            pass
    if not stats["stopped"]:
        queries.set_setting(db, CHECKPOINT_KEY, "")      # finished: the next run starts clean
    have_names = db.query(func.count(models.RegionName.id)).scalar() or 0
    if not hot_only or not have_names:    # weekly names + pruning ride on the daily full run (names: also when empty)
        refresh_region_names(db, accounts[0] if accounts else None, candidates=accounts[1:3])
    if not hot_only:
        prune(db)
    queries.set_setting(db, "audience_synced_at", _now().isoformat())
    queries.set_setting(db, "audience_last_errors", json.dumps(stats["errors"]))
    if aud_days:
        queries.set_setting(db, "audience_breakdown_synced_at", _now().isoformat())
    if hour_days:
        queries.set_setting(db, "audience_hours_synced_at", _now().isoformat())
    db.commit()
    return stats


def prune(db: Session, keep_days: int = KEEP_DAYS) -> int:
    cutoff = (date.fromisoformat(timeutil.local_date_str()) - timedelta(days=keep_days)).isoformat()
    return db.query(models.AudienceStat).filter(models.AudienceStat.date < cutoff).delete()


# Built-in names for the location ids TikTok uses (GeoNames scheme — the same
# ids /tool/region/ returns, e.g. 6252001 = United States). Verified against
# geonames.org. /tool/region/ results override these when available.
GEONAMES_REGIONS: dict[str, str] = {
    # United States
    "4829764": "Alabama", "5879092": "Alaska", "5551752": "Arizona", "4099753": "Arkansas", "5332921": "California",
    "5417618": "Colorado", "4831725": "Connecticut", "4142224": "Delaware", "4138106": "District of Columbia",
    "4155751": "Florida", "4197000": "Georgia", "5855797": "Hawaii", "5596512": "Idaho", "4896861": "Illinois",
    "4921868": "Indiana", "4862182": "Iowa", "4273857": "Kansas", "6254925": "Kentucky", "4331987": "Louisiana",
    "4971068": "Maine", "4361885": "Maryland", "6254926": "Massachusetts", "5001836": "Michigan", "5037779": "Minnesota",
    "4436296": "Mississippi", "4398678": "Missouri", "5667009": "Montana", "5073708": "Nebraska", "5509151": "Nevada",
    "5090174": "New Hampshire", "5101760": "New Jersey", "5481136": "New Mexico", "5128638": "New York",
    "4482348": "North Carolina", "5690763": "North Dakota", "5165418": "Ohio", "4544379": "Oklahoma", "5744337": "Oregon",
    "6254927": "Pennsylvania", "5224323": "Rhode Island", "4597040": "South Carolina", "5769223": "South Dakota",
    "4662168": "Tennessee", "4736286": "Texas", "5549030": "Utah", "5242283": "Vermont", "6254928": "Virginia",
    "5815135": "Washington", "4826850": "West Virginia", "5279468": "Wisconsin", "5843591": "Wyoming",
    # Canada
    "5883102": "Alberta", "5909050": "British Columbia", "6065171": "Manitoba", "6087430": "New Brunswick",
    "6354959": "Newfoundland and Labrador", "6091530": "Nova Scotia", "6091069": "Northwest Territories",
    "6091732": "Nunavut", "6093943": "Ontario", "6113358": "Prince Edward Island", "6115047": "Quebec",
    "6141242": "Saskatchewan", "6185811": "Yukon",
    # United Kingdom
    "6269131": "England", "2641364": "Northern Ireland", "2638360": "Scotland", "2634895": "Wales",
    # countries seen as province_id parents
    "6252001": "United States", "6251999": "Canada", "2635167": "United Kingdom",
}

COUNTRY_NAMES: dict[str, str] = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom", "AU": "Australia", "NZ": "New Zealand",
    "IE": "Ireland", "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy", "NL": "Netherlands",
    "BE": "Belgium", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland",
    "AT": "Austria", "CH": "Switzerland", "PT": "Portugal", "MX": "Mexico", "BR": "Brazil", "AR": "Argentina",
    "CL": "Chile", "CO": "Colombia", "JP": "Japan", "KR": "South Korea", "SG": "Singapore", "MY": "Malaysia",
    "PH": "Philippines", "ID": "Indonesia", "TH": "Thailand", "VN": "Vietnam", "IN": "India", "AE": "United Arab Emirates",
    "SA": "Saudi Arabia", "IL": "Israel", "TR": "Türkiye", "ZA": "South Africa", "EG": "Egypt", "NG": "Nigeria",
    "LV": "Latvia", "LT": "Lithuania", "EE": "Estonia", "CZ": "Czechia", "RO": "Romania", "HU": "Hungary", "GR": "Greece",
}


def refresh_region_names(db: Session, acct: models.AdAccount | None, force: bool = False,
                         candidates: list[models.AdAccount] | None = None) -> int:
    """/tool/region/ once a week → RegionName, so province ids get names.
    Tries up to three accounts (the endpoint is per-advertiser and some
    accounts refuse it); the outcome is kept in a setting for the page."""
    tried = [a for a in ([acct] + list(candidates or [])) if a][:3]
    if not tried:
        return 0
    newest = db.query(func.max(models.RegionName.synced_at)).scalar()
    have = db.query(func.count(models.RegionName.id)).scalar() or 0
    if newest and have and not force and (_now() - newest) < timedelta(days=REGION_REFRESH_DAYS):
        return 0
    raw = None
    last_err = ""
    seen_ids: set[str] = set()
    for a in tried:
        if a.advertiser_id in seen_ids:
            continue
        seen_ids.add(a.advertiser_id)
        try:
            raw = tiktok_api.list_regions(a.access_token, a.advertiser_id, placements=["PLACEMENT_TIKTOK"],
                                          objective_type="TRAFFIC")
            if raw:
                break
            last_err = f"account {a.advertiser_name or a.advertiser_id}: empty region list"
        except tiktok_api.TikTokError as e:
            last_err = f"account {a.advertiser_name or a.advertiser_id}: {e}"
            log.warning("region list failed: %s", e)
    if not raw:
        queries.set_setting(db, "audience_regions_status", f"/tool/region/ gave no names — {last_err or 'no account to ask'}; "
                            f"built-in names cover {len(GEONAMES_REGIONS)} US/CA/UK regions")
        db.commit()
        return 0
    n = 0
    now = _now()
    existing = {r.region_id: r for r in db.query(models.RegionName).all()}
    for item in raw:
        rid = str(item.get("location_id") or item.get("region_id") or item.get("id") or "")
        if not rid:
            continue
        row = existing.get(rid)
        if not row:
            row = models.RegionName(region_id=rid)
            db.add(row)
            existing[rid] = row
        row.name = str(item.get("name") or item.get("region_name") or "")[:120]
        row.level = str(item.get("level") or item.get("area_type") or "")[:40]
        row.parent_id = str(item.get("parent_id") or "")
        row.region_code = str(item.get("region_code") or "")[:20]
        row.synced_at = now
        n += 1
    queries.set_setting(db, "audience_regions_status", f"{n} location names from /tool/region/ ({now.isoformat()[:16]} UTC)")
    db.commit()
    return n


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

AGE_LABELS = {"AGE_13_17": "13–17", "AGE_18_24": "18–24", "AGE_25_34": "25–34", "AGE_35_44": "35–44",
              "AGE_45_54": "45–54", "AGE_55_100": "55+", "AGE_55_64": "55–64", "AGE_65_100": "65+"}
GENDER_LABELS = {"MALE": "Men", "FEMALE": "Women", "NONE": "Unknown"}


def _human(dim: str, key: str, label: str, regions: dict[str, str]) -> str:
    if dim == "age":
        return AGE_LABELS.get(key, key.replace("AGE_", "").replace("_", "–"))
    if dim == "gender":
        return GENDER_LABELS.get(key, key.title() or "Unknown")
    if dim == "age_gender":
        a, _, g = key.partition("|")
        return f"{_human('age', a, '', regions)} · {_human('gender', g, '', regions)}"
    if dim == "province":
        if key in ("", "-1", "None"):
            return "Unknown"
        return regions.get(key, key)
    if dim == "country":
        if key in ("", "None"):
            return "Unknown"
        return f"{COUNTRY_NAMES[key]} ({key})" if key in COUNTRY_NAMES else key
    if dim == "device_brand":
        return label or key or "Unknown"
    if dim == "platform":
        return {"IOS": "iOS", "ANDROID": "Android", "PC": "PC", "OTHER": "Other"}.get(key.upper(), key or "Unknown")
    if dim == "placement":
        return key.replace("PLACEMENT_", "").replace("_", " ").title() if key else "Unknown"
    if dim == "ac":
        return key.replace("_", " ").upper() if key else "Unknown"
    if dim == "language":
        return key or "Unknown"
    return key or "Unknown"


def _base_query(db: Session, dim: str, start: str, end: str, advertiser_ids: list[str] | None,
                campaign_like: str):
    q = db.query(models.AudienceStat).filter(models.AudienceStat.dim == dim,
                                             models.AudienceStat.date >= start, models.AudienceStat.date <= end)
    if advertiser_ids is not None:
        q = q.filter(models.AudienceStat.advertiser_id.in_(advertiser_ids or ["__none__"]))
    if campaign_like:
        q = q.filter(models.AudienceStat.campaign_name.ilike(f"%{campaign_like}%"))
    return q


def breakdown(db: Session, dim: str, start: str, end: str, advertiser_ids: list[str] | None = None,
              campaign_like: str = "", regions: dict[str, str] | None = None, split: str = "") -> list[dict]:
    """Rows for one dimension summed over the range: [{key, label, spend,
    impressions, clicks, conversions, ctr, cpc, cpa, share}] sorted by spend.
    split='age'|'gender' collapses the stored age_gender pairs onto one axis."""
    regions = regions or {}
    store_dim = "age_gender" if dim in ("age", "gender", "age_gender") else dim
    q = _base_query(db, store_dim, start, end, advertiser_ids, campaign_like)
    agg: dict[str, dict] = {}
    for key, label, sp, im, cl, cv in (q.with_entities(
            models.AudienceStat.key, models.AudienceStat.label,
            func.sum(models.AudienceStat.spend), func.sum(models.AudienceStat.impressions),
            func.sum(models.AudienceStat.clicks), func.sum(models.AudienceStat.conversions))
            .group_by(models.AudienceStat.key, models.AudienceStat.label).all()):
        k = key
        if dim == "age":
            k = key.partition("|")[0]
        elif dim == "gender":
            k = key.partition("|")[2]
        row = agg.setdefault(k, {"key": k, "label": _human(dim, k, label or "", regions),
                                 "spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0})
        row["spend"] += float(sp or 0)
        row["impressions"] += int(im or 0)
        row["clicks"] += int(cl or 0)
        row["conversions"] += int(cv or 0)
    rows = list(agg.values())
    total_spend = sum(r["spend"] for r in rows) or 0.0
    total_imp = sum(r["impressions"] for r in rows) or 0
    for r in rows:
        r["ctr"] = (r["clicks"] / r["impressions"] * 100) if r["impressions"] else 0.0
        r["cpc"] = (r["spend"] / r["clicks"]) if r["clicks"] else 0.0
        r["cpa"] = (r["spend"] / r["conversions"]) if r["conversions"] else 0.0
        r["cvr"] = (r["conversions"] / r["clicks"] * 100) if r["clicks"] else 0.0
        r["share"] = (r["spend"] / total_spend * 100) if total_spend else 0.0
        r["imp_share"] = (r["impressions"] / total_imp * 100) if total_imp else 0.0
    rows.sort(key=lambda r: (-r["spend"], -r["impressions"], r["label"]))
    return rows


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def heatmap(db: Session, start: str, end: str, advertiser_ids: list[str] | None = None,
            campaign_like: str = "", metric: str = "spend") -> dict:
    """weekday × hour grid of one metric (summed over the range), plus per-hour
    and per-weekday totals and the max for colouring."""
    col = {"spend": models.AudienceStat.spend, "impressions": models.AudienceStat.impressions,
           "clicks": models.AudienceStat.clicks, "conversions": models.AudienceStat.conversions}.get(metric, models.AudienceStat.spend)
    q = _base_query(db, HOUR, start, end, advertiser_ids, campaign_like)
    grid = [[0.0] * 24 for _ in range(7)]
    days_seen: set[str] = set()
    for day, key, v in (q.with_entities(models.AudienceStat.date, models.AudienceStat.key, func.sum(col))
                        .group_by(models.AudienceStat.date, models.AudienceStat.key).all()):
        try:
            wd = date.fromisoformat(day).weekday()
            h = int(key)
        except ValueError:
            continue
        if 0 <= h < 24:
            grid[wd][h] += float(v or 0)
            days_seen.add(day)
    by_hour = [sum(grid[w][h] for w in range(7)) for h in range(24)]
    by_day = [sum(grid[w]) for w in range(7)]
    mx = max((v for row in grid for v in row), default=0.0)
    return {"grid": grid, "by_hour": by_hour, "by_day": by_day, "max": mx, "days": len(days_seen),
            "hour_max": max(by_hour, default=0.0), "metric": metric, "weekdays": WEEKDAYS}


def region_names(db: Session) -> dict[str, str]:
    """id → name: the built-in GeoNames table, overridden by whatever
    /tool/region/ returned."""
    out = dict(GEONAMES_REGIONS)
    out.update({r.region_id: r.name for r in db.query(models.RegionName.region_id, models.RegionName.name).all() if r.name})
    return out


def coverage(db: Session) -> dict:
    """What the store holds: date span, rows, accounts, last sync, the last
    refresh job (queued / running / finished) and the accounts that failed."""
    lo, hi, n = db.query(func.min(models.AudienceStat.date), func.max(models.AudienceStat.date),
                         func.count(models.AudienceStat.id)).one()
    accts = db.query(func.count(func.distinct(models.AudienceStat.advertiser_id))).scalar() or 0
    job = db.query(models.Job).filter(models.Job.kind == "audience_sync").order_by(models.Job.id.desc()).first()
    try:
        errors = json.loads(queries.get_setting(db, "audience_last_errors", "[]") or "[]")
    except ValueError:
        errors = []
    return {"first": lo, "last": hi, "rows": int(n or 0), "accounts": int(accts),
            "synced_at": queries.get_setting(db, "audience_synced_at", ""), "job": job, "errors": errors,
            "regions": queries.get_setting(db, "audience_regions_status", "")}
