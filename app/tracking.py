"""Click tracking — the RedTrack / ClickFlare model, built in.

    ad click → lander (?source=<campaign name>&ttclid=…&tt_cid=…&tt_aid=…&tt_ad=…)
             → the click is registered here and gets a SHORT click id (12 chars)
             → offer link carries  source=<campaign name>~<click id>
             → Glitchy echoes {source} in its postback
             → the click id resolves back to the TikTok click (ttclid, ip, user agent,
               campaign / ad group / ad ids) → Events API + exact attribution.

Two ways the click gets registered, same as the trackers offer:
  direct    the pass-source.js script on the prelander/lander calls POST /t/click
            (no redirect, the ad lands straight on your page)
  redirect  the ad's destination is /t/c?to=<lander>&… — the click is recorded
            server-side and the visitor is redirected to the lander with the ids

Only a 12-character id has to survive the network round trip — nothing long
or exotic — which is exactly why the trackers work with every network.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from . import models

CLICK_ID_RE = re.compile(r"^[a-z0-9]{12}$")
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
SEP = "~"

# the TikTok macros the launcher appends to every landing URL, and the short
# query params they arrive in (kept short — they ride every hop to the offer)
TT_MACROS = (("tt_cid", "__CAMPAIGN_ID__"), ("tt_aid", "__AID__"), ("tt_ad", "__CID__"))


def new_click_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(12))


def is_click_id(v: str) -> bool:
    return bool(v) and bool(CLICK_ID_RE.match(v))


def _clean(v, n: int) -> str:
    v = (v or "").strip()
    return "" if ("{" in v or "}" in v or v.startswith("__")) else v[:n]     # unreplaced macros never get stored


def record_click(db: Session, *, source: str, ttclid: str = "", tt_campaign_id: str = "", tt_adgroup_id: str = "",
                 tt_ad_id: str = "", ip: str = "", user_agent: str = "", url: str = "", referrer: str = "",
                 how: str = "direct") -> models.Click:
    """Store one click; the advertiser is resolved from the campaign id (exact) or the
    campaign name (what the launcher named it) so P&L and the Events API can pick the
    right account's pixel and token."""
    source = _clean(source, 200)
    tt_campaign_id = _clean(tt_campaign_id, 40)
    adv = ""
    if tt_campaign_id:
        rec = db.query(models.CampaignRecord.advertiser_id).filter_by(campaign_id=tt_campaign_id).first()
        adv = rec[0] if rec else ""
    if not adv and source:
        rec = (db.query(models.CampaignRecord.advertiser_id).filter_by(campaign_name=source)
               .order_by(models.CampaignRecord.id.desc()).first())
        adv = rec[0] if rec else ""
    for _ in range(3):
        cid = new_click_id()
        if not db.query(models.Click.id).filter_by(click_id=cid).first():
            break
    row = models.Click(click_id=cid, source=source, ttclid=_clean(ttclid, 500), tt_campaign_id=tt_campaign_id,
                       tt_adgroup_id=_clean(tt_adgroup_id, 40), tt_ad_id=_clean(tt_ad_id, 40), advertiser_id=adv,
                       ip=(ip or "")[:64], user_agent=(user_agent or "")[:300], url=(url or "")[:1000],
                       referrer=(referrer or "")[:500], how=how)
    db.add(row)
    db.flush()
    return row


def lookup(db: Session, click_id: str) -> models.Click | None:
    if not is_click_id(click_id):
        return None
    return db.query(models.Click).filter_by(click_id=click_id).first()


def with_params(url: str, params: dict) -> str:
    """Append/replace query params on a URL (macros like __CAMPAIGN_ID__ pass through unencoded — TikTok
    only substitutes them when they appear literally)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query.extend((k, v) for k, v in params.items() if v is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, safe="_{}"), parts.fragment))


def ad_url(landing_url: str, url_param: str, source_value: str, mode: str, base: str) -> str:
    """The destination URL to put on the ad.
    direct / clickflare → <lander>?source=…&tt_cid=__CAMPAIGN_ID__&tt_aid=__AID__&tt_ad=__CID__
                          (with ClickFlare the lander is your ClickFlare campaign URL / lander with cpid=;
                          map `source` to a tracking field in the traffic source and it comes back in the postback)
    redirect            → <base>/t/c?to=<lander>&source=…&tt_cid=…   (ttclid is appended by TikTok either way)"""
    if not landing_url:
        return landing_url
    params = {url_param: source_value}
    params.update(dict(TT_MACROS))
    if mode == "redirect" and base:
        return with_params(base.rstrip("/") + "/t/c", {"to": landing_url, **params})
    return with_params(landing_url, params)


def base_url(db: Session, s: dict | None = None) -> str:
    """Public https base the ads/landers reach this app on: Settings → tracking domain, else the
    postback hostname, else the host the dashboard was last opened on."""
    from . import config, queries
    from .settings_store import get_settings
    s = s or get_settings(db)
    host = (s.get("tracking_domain") or "").strip() or config.POSTBACK_HOST or queries.get_setting(db, "public_host", "")
    return ("https://" + host) if host else ""


def allowed_redirect(db: Session, to: str) -> bool:
    """/t/c only redirects to hosts you actually launch to (every preset's landing URL) —
    never an open redirect for strangers."""
    try:
        host = urlsplit(to).netloc.lower().split(":")[0]
    except ValueError:
        return False
    if not host or urlsplit(to).scheme not in ("http", "https"):
        return False
    import json
    hosts = set()
    for (blob,) in db.query(models.Template.adgroup_settings):
        try:
            u = (json.loads(blob or "{}") or {}).get("landing_page_url") or ""
        except ValueError:
            u = ""
        if u:
            hosts.add(urlsplit(u).netloc.lower().split(":")[0])
    for (u,) in db.query(models.LaunchLog.landing_url).filter(models.LaunchLog.landing_url != "").distinct().limit(500):
        hosts.add(urlsplit(u).netloc.lower().split(":")[0])
    return host in hosts or any(host.endswith("." + h) for h in hosts if h)


def stats(db: Session, days: int = 1) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    q = db.query(models.Click).filter(models.Click.created_at >= since)
    total = q.count()
    with_tt = q.filter(models.Click.ttclid != "").count()
    conv = q.filter(models.Click.converted_at.isnot(None)).count()
    last = db.query(models.Click).order_by(models.Click.id.desc()).first()
    return {"total": total, "with_ttclid": with_tt, "converted": conv, "last": last}
