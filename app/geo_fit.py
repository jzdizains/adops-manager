"""Can this ad account target these countries? — checked before anything is created.

TikTok unlocks countries per ad account (/tool/region/ lists what one account may target:
some see 33 countries, others 65). A launch aimed at a country the account can't target was
refused at ad-group creation — after the campaign existed. Now the account's targetable
countries are read once a week (cached in a Setting row per account) and a preset's
country-level locations are checked against them up front. Unknown (never read, or the read
failed) passes: missing data must never block a launch. Province / city ids are only checked
when we know their country.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

REFRESH = timedelta(days=7)
US_LOCATION_ID = "6252001"
UNKNOWN_NON_US = "?"          # a location we couldn't place in a country — at least not the US


def fit(targetable: list[str] | None, wanted: list[str], country_of=lambda i: i) -> tuple[bool, list[str]]:
    """(ok, the wanted country ids the account can't target). Pure."""
    if not targetable or not wanted:
        return True, []
    known = set(str(x) for x in targetable)
    missing = []
    for w in wanted:
        c = country_of(str(w))
        if c and c not in known and c not in missing:
            missing.append(c)
    return (not missing), missing


def _key(adv: str) -> str:
    return f"acct_regions:{adv}"


def targetable(db, models, acct, objective: str = "", tiktok_api=None) -> list[str] | None:
    """The account's targetable COUNTRY location ids (cached a week), None when unknown."""
    from . import queries
    raw = queries.get_setting(db, _key(acct.advertiser_id), "")
    if raw:
        try:
            d = json.loads(raw)
            if datetime.utcnow() - datetime.fromisoformat(d.get("at", "2000-01-01")) < REFRESH:
                return d.get("countries") or None
        except (ValueError, TypeError):
            pass
    if tiktok_api is None:
        from . import tiktok_api as _t
        tiktok_api = _t
    if not acct.access_token:
        return None
    try:
        items = tiktok_api.list_regions(acct.access_token, acct.advertiser_id, placements=["PLACEMENT_TIKTOK"],
                                        objective_type=objective or None) or []
    except tiktok_api.TikTokError:
        return None
    countries = sorted({str(i.get("location_id") or i.get("region_id") or i.get("id") or "") for i in items
                        if str(i.get("level") or i.get("area_type") or i.get("region_level") or "").upper() == "COUNTRY"} - {""})
    try:
        queries.set_setting(db, _key(acct.advertiser_id), json.dumps({"at": datetime.utcnow().isoformat(timespec="seconds"),
                                                                        "countries": countries}))
    except Exception:      # noqa: BLE001
        db.rollback()
    return countries or None


_RES: dict = {"at": 0.0, "rows": None}


def country_resolver(db, models):
    """location id → its country id via the synced region names (None = unknown). The table is
    read once per 10 minutes per process (a batch launch asks for every account)."""
    import time as _t
    if _RES["rows"] is None or _t.time() - _RES["at"] > 600:
        _RES["rows"] = {r.region_id: (r.level or "", r.parent_id or "")
                        for r in db.query(models.RegionName.region_id, models.RegionName.level, models.RegionName.parent_id)}
        _RES["at"] = _t.time()
    rows = _RES["rows"]

    def country_of(loc: str):
        seen = 0
        cur = str(loc)
        while cur and seen < 6:
            lvl, parent = rows.get(cur, ("", ""))
            if not lvl:
                return None
            if lvl.upper() == "COUNTRY":
                return cur
            cur, seen = parent, seen + 1
        return None
    return country_of


def names(db, models, ids: list[str]) -> str:
    got = {r.region_id: r.name for r in db.query(models.RegionName).filter(models.RegionName.region_id.in_(ids or [""]))}
    return ", ".join(got.get(i) or i for i in ids)


# ---------------------------------------------------------------------------------------------
# Geo policy (v151): what an account is FOR, not only what it CAN target. US access is the scarce
# thing — "auto" keeps US-unlocked accounts for US launches and sends everything else to the rest.
# ---------------------------------------------------------------------------------------------
POLICIES = [
    ("any", "Any geo", "Whatever it can target (default)"),
    ("auto", "Auto", "US unlocked → US launches only; otherwise everything but the US"),
    ("us_only", "US only", "Never used for a non-US launch"),
    ("non_us", "Non-US", "Never used for a US launch"),
]
POLICY_KEYS = tuple(k for k, _, _ in POLICIES)


def policy_fit(policy: str | None, targetable: list[str] | None, wanted_countries: list[str]) -> tuple[bool, str, str]:
    """(ok, kind "cannot"|"policy"|"", reason) for a launch aimed at `wanted_countries` (country
    location ids). Unknown targetable countries never block "auto". Pure."""
    wanted = [str(w) for w in dict.fromkeys(wanted_countries or []) if w]
    if not wanted:
        return True, "", ""
    wants_us = US_LOCATION_ID in wanted
    only_us = wants_us and len(wanted) == 1
    known = set(str(x) for x in targetable) if targetable else None
    policy = policy if policy in POLICY_KEYS else "any"
    if policy == "us_only" and not only_us:
        return False, "policy", "set to US only — kept for US launches"
    if policy == "non_us" and wants_us:
        return False, "policy", "set to Non-US — kept for non-US launches"
    if policy == "auto" and known is not None:
        has_us = US_LOCATION_ID in known
        if has_us and not only_us:
            return False, "policy", "US-unlocked account (Auto) — kept for US launches"
        if not has_us and wants_us:
            return False, "cannot", "US targeting isn't unlocked on this account"
    return True, "", ""


def cached_targetable(db, advertiser_id: str) -> list[str] | None:
    """The account's targetable countries from the weekly cache only — never calls TikTok
    (for the review table and auto-pick, which look at many accounts at once)."""
    from . import queries
    raw = queries.get_setting(db, _key(advertiser_id), "")
    if not raw:
        return None
    try:
        return json.loads(raw).get("countries") or None
    except (ValueError, TypeError, AttributeError):
        return None


def targetable_map(db, models, advertiser_ids) -> dict:
    """{advertiser: countries or None} from the weekly cache for many accounts in a few queries
    (the review table, auto-pick) — never one query per account."""
    ids = [str(i) for i in advertiser_ids or []]
    out = {i: None for i in ids}
    for n in range(0, len(ids), 500):
        keys = [_key(i) for i in ids[n:n + 500]]
        for k, v in db.query(models.Setting.key, models.Setting.value).filter(models.Setting.key.in_(keys)):
            try:
                out[k.split(":", 1)[1]] = json.loads(v or "{}").get("countries") or None
            except (ValueError, TypeError, AttributeError):
                pass
    return out


def wanted_countries(db, models, location_ids) -> list[str]:
    """A preset's location ids → their country ids (unknown provinces/cities are left out)."""
    country_of = country_resolver(db, models)
    out = []
    unknown = False
    for loc in location_ids or []:
        c = country_of(str(loc)) or (US_LOCATION_ID if str(loc) == US_LOCATION_ID else None)
        if c and c not in out:
            out.append(c)
        elif not c:
            unknown = True
    if unknown and not out:
        # none of them resolved (regions not synced yet): it isn't a US launch — say "somewhere
        # else" so US-only / Auto-US accounts are still kept for US launches
        out.append(UNKNOWN_NON_US)
    elif unknown and US_LOCATION_ID in out:
        out.append(UNKNOWN_NON_US)          # US + something unresolved = not US-only
    return out


def badge(countries: list[str] | None) -> str:
    """"US ✓ · 65 countries" / "no US · 33 countries" / "" when unknown. Pure."""
    if not countries:
        return ""
    return f"{'US ✓' if US_LOCATION_ID in countries else 'no US'} · {len(countries)} {'country' if len(countries) == 1 else 'countries'}"
