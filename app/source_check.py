"""Source Check — proves, from TikTok's side, whether every live campaign can be
attributed by Glitchy, and repairs the ones that can't.

For each tool-launched campaign that is running, the ads' REAL landing page
URLs are pulled with /ad/get/ (not what we intended to send — what TikTok has).
Each ad gets a verdict:

  ok            ?source=__CAMPAIGN_NAME__ and the name is URL-safe  → source arrives
  ok-static     ?source=<fixed value>                               → arrives (static mode)
  unsafe-name   macro present but the campaign name has spaces/symbols — TikTok
                doesn't URL-encode substituted macros, so the value is corrupt
  missing       no ?source= at all (launched before source wiring existed)
  no-url        the ad has no landing URL (instant page / lead form) — cannot carry a source

`fix_campaign` repairs a campaign in place: renames it URL-safe when needed
(moving the join key + booked revenue with it, like the Edit page does) and
rewrites every ad's landing URL to carry the source. TikTok re-reviews an ad
whose URL changes — that's TikTok's rule, so the page says so.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.orm import Session

from . import models, tiktok_api
from .settings_store import get_settings

MACRO = "__CAMPAIGN_NAME__"


def _url_safe(name: str) -> bool:
    import re
    return bool(name) and re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None


def verdict(landing_url: str, param: str, campaign_name: str) -> tuple[str, str]:
    """(code, human explanation) for one ad's landing URL."""
    if not landing_url:
        return "no-url", "This ad has no landing page URL (instant page / lead form) — a source can't ride it."
    q = dict(parse_qsl(urlsplit(landing_url).query, keep_blank_values=True))
    val = q.get(param)
    if val is None or val == "":
        return "missing", f"The landing URL carries no ?{param}= — every click reaches Glitchy unattributed."
    if MACRO in val:
        if _url_safe(campaign_name):
            return "ok", f"TikTok fills in the campaign name at click time → ?{param}={campaign_name}"
        return "unsafe-name", (f"The macro is there, but the campaign name “{campaign_name}” has spaces/symbols — "
                               "TikTok substitutes it raw, which breaks the URL. Rename it URL-safe.")
    return "ok-static", f"Fixed value ?{param}={val}"


def audit(db: Session, only_active: bool = True) -> list[dict]:
    """One row per tool-launched campaign with its ads' verdicts (live from TikTok).
    Never raises — an account that can't be read gets an 'error' row."""
    s = get_settings(db)
    param = s.get("url_param") or "source"
    recs = {(r.advertiser_id, r.campaign_id): r for r in db.query(models.CampaignRecord).all()}
    accts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    # tool-launched campaigns (one LaunchLog per campaign is enough)
    logs: dict[tuple[str, str], models.LaunchLog] = {}
    for lg in (db.query(models.LaunchLog).filter(models.LaunchLog.ok == True)  # noqa: E712
               .filter(models.LaunchLog.campaign_id != "").order_by(models.LaunchLog.id.desc())):
        logs.setdefault((lg.advertiser_id, lg.campaign_id), lg)
    by_acct: dict[str, list[tuple[str, models.LaunchLog]]] = {}
    for (aid, cid), lg in logs.items():
        rec = recs.get((aid, cid))
        if only_active and rec is not None and rec.operation_status != "ENABLE":
            continue
        by_acct.setdefault(aid, []).append((cid, lg))

    rows: list[dict] = []
    for aid, items in by_acct.items():
        acct = accts.get(aid)
        cids = [cid for cid, _ in items]
        ads_by_cid: dict[str, list[dict]] = {}
        err = ""
        if not acct or not acct.access_token:
            err = "no token for this account"
        else:
            try:
                page = 1
                while True:
                    data = tiktok_api.list_ads(acct.access_token, aid, page=page, page_size=100,
                                               filtering={"campaign_ids": cids[:100]})
                    for ad in data.get("list", []):
                        ads_by_cid.setdefault(str(ad.get("campaign_id", "")), []).append(ad)
                    info = data.get("page_info", {}) or {}
                    if page >= int(info.get("total_page", 1) or 1) or page >= 20:
                        break
                    page += 1
            except tiktok_api.TikTokError as e:
                err = f"TikTok code {e.code}: {e.message[:120]}"
        for cid, lg in items:
            rec = recs.get((aid, cid))
            name = (rec.campaign_name if rec else "") or lg.source or ""
            ads = []
            worst = None
            rank = {"ok": 0, "ok-static": 0, "unsafe-name": 2, "missing": 3, "no-url": 1, "error": 4}
            for ad in ads_by_cid.get(cid, []):
                url = ad.get("landing_page_url") or ""
                code, why = verdict(url, param, name)
                ads.append({"ad_id": str(ad.get("ad_id", "")), "adgroup_id": str(ad.get("adgroup_id", "")),
                            "ad_name": ad.get("ad_name", ""), "url": url, "code": code, "why": why,
                            "status": ad.get("secondary_status", "")})
                if worst is None or rank[code] > rank[worst]:
                    worst = code
            worst = worst or "ok"
            if err:
                worst = "error"
            elif not ads:
                worst = "error"
                err = "TikTok returned no ads for this campaign (deleted, or not visible to this token)"
            rows.append({
                "advertiser_id": aid, "advertiser_name": (acct.advertiser_name if acct else aid),
                "campaign_id": cid, "campaign_name": name, "status": rec.operation_status if rec else "",
                "spend": float(rec.spend_today or 0) if rec else 0.0,
                "source": lg.source or "", "ads": ads, "worst": worst, "error": err,
                # a click-through you can test in a browser: macro replaced by the real name
                "test_url": (ads[0]["url"].replace(MACRO, name) if ads and ads[0]["url"] else ""),
            })
    order = {"missing": 0, "unsafe-name": 1, "error": 2, "no-url": 3, "ok-static": 4, "ok": 5}
    rows.sort(key=lambda r: (order.get(r["worst"], 9), -r["spend"]))
    return rows


def fix_campaign(db: Session, advertiser_id: str, campaign_id: str) -> tuple[list[str], list[str]]:
    """Make one campaign attributable. Returns (changes, errors)."""
    from .routes.campaigns import apply_source_to_url, url_safe_name
    s = get_settings(db)
    param = s.get("url_param") or "source"
    mode = s.get("source_mode", "campaign")
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    if not acct or not acct.access_token:
        return [], ["no token for this account"]
    rec = (db.query(models.CampaignRecord)
           .filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id).first())
    logs = list(db.query(models.LaunchLog).filter_by(advertiser_id=advertiser_id, campaign_id=campaign_id))
    changes, errors = [], []
    name = (rec.campaign_name if rec else "") or ""

    # 1. campaign-name mode needs a URL-safe name — rename on TikTok + move the join key
    if mode == "campaign" and name and not _url_safe(name):
        new_name = url_safe_name(name)
        # keep it unique per account, like the launcher does
        taken = {r.campaign_name for r in db.query(models.CampaignRecord).filter_by(advertiser_id=advertiser_id)}
        if new_name in taken:
            new_name = f"{new_name}_{campaign_id[-4:]}"
        try:
            tiktok_api.update_campaign_name(acct.access_token, advertiser_id, campaign_id, new_name,
                                            smart_plus=bool(rec.is_smart_plus) if rec else False)
            old = name
            if rec:
                rec.campaign_name = new_name
            for lg in logs:
                if (lg.source or "") in ("", old):
                    lg.source = new_name
            if old:
                db.query(models.PostbackEvent).filter_by(source=old).update({"source": new_name})
            name = new_name
            changes.append(f"renamed → {new_name}")
        except tiktok_api.TikTokError as e:
            errors.append(f"rename: code {e.code} {e.message[:100]}")
            return changes, errors

    # 2. what the source value must be
    if mode == "campaign":
        value = MACRO
        join_key = name
    else:
        join_key = next((lg.source for lg in logs if lg.source), "") or (name and url_safe_name(name)) or f"c{campaign_id[-6:]}"
        value = join_key

    # 3. rewrite every ad whose URL doesn't carry it
    try:
        data = tiktok_api.list_ads(acct.access_token, advertiser_id, page_size=100,
                                   filtering={"campaign_ids": [campaign_id]})
    except tiktok_api.TikTokError as e:
        errors.append(f"reading ads: code {e.code} {e.message[:100]}")
        return changes, errors
    fixed = 0
    for ad in data.get("list", []):
        url = ad.get("landing_page_url") or ""
        if not url:
            continue
        code, _ = verdict(url, param, name)
        if code in ("ok", "ok-static"):
            continue
        new_url = apply_source_to_url(url, param, value)
        try:
            tiktok_api.update_ad_landing_url(acct.access_token, advertiser_id,
                                             str(ad.get("adgroup_id", "")), str(ad.get("ad_id", "")), new_url)
            fixed += 1
        except tiktok_api.TikTokError as e:
            errors.append(f"ad {str(ad.get('ad_id', ''))[-6:]}: code {e.code} {e.message[:100]}")
    if fixed:
        changes.append(f"{fixed} ad(s) now carry ?{param}={'<campaign name>' if value == MACRO else value}")
        for lg in logs:
            if not lg.source:
                lg.source = join_key
            lg.landing_url = apply_source_to_url(lg.landing_url or "", param, value) if lg.landing_url else lg.landing_url
    db.commit()
    return changes, errors
