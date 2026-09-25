"""The Review step's per-account check (v151): before anything is created, one row per target
account — its page / form / display card / identity / geo / start time — and whether it is
BLOCKED (with the reason). Blocked accounts can be left out of the launch in one click.

Read from what we already know (synced pages and forms, the build queue, the weekly geo cache,
card uploads, account status): no TikTok calls, so it answers instantly for 50 accounts.
Two things need TikTok and are opt-in: identities (`identities()`, fetched a few accounts at a
time after the table is drawn) and a live re-check of pages / forms the last sync didn't see
(`live=True`, only for the rows that are missing one).
"""
from __future__ import annotations

import time

OK, WARN, BAD, NA = "ok", "warn", "bad", "na"


def cell(state: str, text: str, hint: str = "") -> dict:
    return {"state": state, "text": text, "hint": hint}


def verdict(cells: dict) -> tuple[str, list[str]]:
    """(blocked reason or "", warnings) from a row's cells. The first BAD cell (in column order)
    is the reason. Pure."""
    order = ("account", "page", "form", "terms", "card", "identity", "geo")
    blocked = ""
    warns = []
    for k in order:
        c = cells.get(k)
        if not c:
            continue
        if c["state"] == BAD and not blocked:
            blocked = c.get("hint") or c["text"]
        elif c["state"] == WARN:
            warns.append(c.get("hint") or c["text"])
    return blocked, warns


def asset_name(db, models, fields: dict, kind: str, owner=None) -> tuple[str, object]:
    """The page / form name a launch resolves on each account, and the template (page template or
    form template) that can build it — the same rules launch_to_account uses. Pure-ish (reads)."""
    if kind == "page":
        name = fields.get("instant_page_name") or ""
        if not name and fields.get("instant_page_id"):
            legacy = db.query(models.InstantPage).filter_by(page_id=fields["instant_page_id"]).first()
            name = legacy.name if legacy else ""
        tpl = db.get(models.PageTemplate, int(fields["page_template_id"])) if str(fields.get("page_template_id") or "").isdigit() else None
        if not name and tpl is not None:
            name = tpl.name
        return name, (tpl if tpl is not None and tpl.name == name else None)
    name = fields.get("lead_form_name") or ""
    if not name and fields.get("lead_form_id"):
        legacy = db.query(models.LeadForm).filter_by(form_id=fields["lead_form_id"]).first()
        name = legacy.name if legacy else ""
    ft = None
    if name:
        # the launch only builds from a template of the launcher's own workspace
        ft = (db.query(models.FormTemplate).filter(models.FormTemplate.name == name, models.FormTemplate.master_form_id != "",
                                                  models.FormTemplate.owner_user_id == owner).first())
    return name, ft


def review(db, models, fields: dict, accounts: list, spark=None, identity: str = "account", live: bool = False) -> dict:
    terms_auto = terms_auto_for(db, fields.get("_launched_by"))
    try:
        from . import settings_store as _ss
        start_back = int(_ss.get_settings(db, fields.get("_launched_by")).get("start_back_min") or 0)
    except Exception:  # noqa: BLE001
        start_back = 0
    """{rows, blocked, ready, warned, asset: {page, form}} for a synthesized preset `fields`.
    identity: "spark" (one spark code — `spark`), "post" (each picked post's own profile), or
    "account" (library / carousel: the account's own identity, filled in by identities())."""
    from . import acct_time, geo_fit
    from .routes import super_launcher as sl
    from . import rules as rules_mod

    dest = fields.get("destination_type") or ""
    use_carousel = fields.get("creative_source") in ("carousel", "image")     # no display card on either
    ids = [a.advertiser_id for a in accounts]
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    punished = sl.account_level_blocks(db)

    # ---- pages / forms (one query each, for every account) ----------------------------------
    page_name, page_tpl = asset_name(db, models, fields, "page") if dest == "instant_page" else ("", None)
    form_name, _ = asset_name(db, models, fields, "form", fields.get("_launched_by")) if dest == "lead_form" else ("", None)
    _ft: dict = {}

    def form_tpl_for(a):
        """The form template the launch would build from on this account (same rule as the launch)."""
        owner = fields.get("_launched_by") if fields.get("_launched_by") is not None else a.owner_user_id
        if owner not in _ft:
            _ft[owner] = asset_name(db, models, fields, "form", owner)[1]
        return _ft[owner]
    have_page, have_form, sib_owners, building = {}, {}, set(), {}
    if page_name:
        for r in db.query(models.InstantPage).filter(models.InstantPage.name == page_name,
                                                     models.InstantPage.owner_advertiser_id.in_(ids or [""])):
            have_page[r.owner_advertiser_id] = r
        # a published page of that name on an account of the same workspace (what the launch copies)
        sib_owners = {o for (o,) in (db.query(models.AdAccount.owner_user_id)
                                     .join(models.InstantPage, models.InstantPage.owner_advertiser_id == models.AdAccount.advertiser_id)
                                     .filter(models.InstantPage.name == page_name, models.InstantPage.status == "PUBLISHED").distinct())}
    if form_name:
        for r in db.query(models.LeadForm).filter(models.LeadForm.name == form_name,
                                                  models.LeadForm.owner_advertiser_id.in_(ids or [""])):
            have_form[r.owner_advertiser_id] = r
    if page_name or form_name:
        for b in (db.query(models.AssetBuild).filter(models.AssetBuild.advertiser_id.in_(ids or [""]),
                                                     models.AssetBuild.name.in_([n for n in (page_name, form_name) if n]),
                                                     models.AssetBuild.status.in_(("pending", "running")))):
            building[(b.kind, b.advertiser_id)] = b
    cookies = None

    def has_cookies() -> bool:
        nonlocal cookies
        if cookies is None:
            try:
                from . import spark_web_api
                cookies = bool(spark_web_api.load_cookies())
            except Exception:      # noqa: BLE001
                cookies = False
        return cookies

    # ---- display card ------------------------------------------------------------------------
    card = None
    card_up = {}
    if fields.get("display_card_id") and not use_carousel:
        card = db.get(models.DisplayCard, int(fields["display_card_id"])) if str(fields["display_card_id"]).isdigit() else None
        if card is not None:
            for u in db.query(models.DisplayCardUpload).filter(models.DisplayCardUpload.card_id == card.id,
                                                               models.DisplayCardUpload.advertiser_id.in_(ids or [""])):
                card_up[u.advertiser_id] = u

    # ---- geo -----------------------------------------------------------------------------------
    locs = [] if fields.get("account_default_geo") else [str(x) for x in fields.get("location_ids") or []]
    wanted = geo_fit.wanted_countries(db, models, locs) if locs else []
    country_of = geo_fit.country_resolver(db, models) if locs else None
    known_map = geo_fit.targetable_map(db, models, ids) if locs else {}      # one read for every row
    names_cache: dict = {}

    main_bc = sl_main_bc(db) if spark is not None else ""
    rows = []
    for a in accounts:
        aid = a.advertiser_id
        cells = {}
        # account
        why = sl.block_reason(a, bcs.get(a.owner_bc_id or ""), punished)
        if not a.enabled:
            cells["account"] = cell(BAD, "switched off", "account is switched off")
        elif not a.access_token:
            cells["account"] = cell(BAD, "no token", "no TikTok token on this account — reconnect it")
        elif why:
            cells["account"] = cell(BAD, why, why)
        elif rules_mod.in_cooldown(a):
            cells["account"] = cell(WARN, "cooling down", "cooling down after failed launches")
        else:
            cells["account"] = cell(OK, "ok")
        # page
        if page_name:
            r = have_page.get(aid)
            b = building.get(("page", aid))
            if r is not None:
                st = (r.status or "").upper()
                cells["page"] = cell(OK if st in ("", "PUBLISHED") else WARN, page_name,
                                     "" if st in ("", "PUBLISHED") else f"page “{page_name}” is {st.lower()}")
                cells["page"]["id"] = r.page_id
            elif b is not None:
                cells["page"] = cell(WARN, "building now", f"“{page_name}” is being built on it ({b.step or 'queued'})")
            elif page_tpl is not None:
                cells["page"] = cell(WARN, "built at launch", f"“{page_name}” is built from its template when it launches")
            elif a.owner_user_id in sib_owners and has_cookies():
                cells["page"] = cell(WARN, "copied at launch", f"“{page_name}” is copied from another account when it launches")
            else:
                cells["page"] = cell(BAD, "missing", f"no Instant Page “{page_name}” on this account"
                                     + (" (copying one needs the TikTok cookies)" if a.owner_user_id in sib_owners else ""))
        elif dest == "instant_page":
            cells["page"] = cell(BAD, "none picked", "the preset has no Instant Page")
        # form
        if form_name:
            r = have_form.get(aid)
            b = building.get(("form", aid))
            if r is not None:
                cells["form"] = cell(OK, form_name)
                cells["form"]["id"] = r.form_id
            elif b is not None:
                cells["form"] = cell(WARN, "building now", f"“{form_name}” is being built on it ({b.step or 'queued'})")
            elif form_tpl_for(a) is not None:
                cells["form"] = cell(WARN, "built at launch", f"“{form_name}” is built from its form template when it launches")
            else:
                cells["form"] = cell(BAD, "missing", f"no lead form “{form_name}” on this account")
        elif dest == "lead_form":
            cells["form"] = cell(BAD, "none picked", "the preset has no lead form")
        # display card
        if fields.get("display_card_id") and not use_carousel:
            if card is None:
                cells["card"] = cell(BAD, "deleted", "the preset's display card no longer exists")
            else:
                u = card_up.get(aid)
                cells["card"] = (cell(OK, card.name or "card") if (u and u.portfolio_id and u.upload_md5 == card.md5)
                                 else cell(OK, "uploaded at launch", f"“{card.name}” is uploaded to it at launch"))
        # identity (spark: known now; library: fetched after — see identities())
        if identity == "spark" and spark is not None:
            if (spark.status or "") == "expired" or (spark.check_state or "") == "bad":
                cells["identity"] = cell(BAD, "spark code bad", f"spark code “{spark.name}”: {spark.check_error or 'expired / refused by TikTok'}"[:200])
            elif spark.identity_bc_id and spark.identity_bc_id not in (a.owner_bc_id or "", main_bc):
                cells["identity"] = cell(WARN, "other BC's profile", "the post's profile belongs to another Business Center — TikTok may refuse it")
            else:
                cells["identity"] = cell(OK, "post owner", "the creator's post runs under their identity")
        elif identity in ("spark", "post"):
            cells["identity"] = cell(NA, "each post's owner")
        else:
            cells["identity"] = cell(NA, "checking…")
        # geo
        if locs:
            known = known_map.get(aid)
            ok_fit, missing = geo_fit.fit(known, locs, country_of)
            if not ok_fit:
                mk = tuple(missing)
                if mk not in names_cache:
                    names_cache[mk] = geo_fit.names(db, models, missing)
                cells["geo"] = cell(BAD, "can't target", f"TikTok hasn't unlocked {names_cache[mk]} for it")
            else:
                good, _kind, why_pol = geo_fit.policy_fit(getattr(a, "geo_policy", "") or "any", known, wanted)
                pol = getattr(a, "geo_policy", "") or "any"
                label = dict((k, l) for k, l, _ in geo_fit.POLICIES).get(pol, "Any geo")
                if not good:
                    cells["geo"] = cell(BAD, label, why_pol)
                else:
                    cells["geo"] = cell(OK, geo_fit.badge(known) or label, "" if known else "its countries haven't been read yet — checked at launch")
        else:
            cells["geo"] = cell(NA, "own country" if fields.get("account_default_geo") else "—")
        # TikTok's Lead Generation Terms (v155.13): an Instant Form ad is refused on an account where
        # they aren't accepted; read lazily (terms_cell), a known answer is shown at once
        terms_pending = False
        if dest == "lead_form":
            from . import lead_terms
            known_terms = lead_terms.cached(aid)
            if known_terms is None:
                cells["terms"] = cell(NA, "checking…")
                terms_pending = True
            else:
                cells["terms"] = terms_cell(known_terms, auto=terms_auto)
        blocked, warns = verdict(cells)
        tz = a.timezone or ""
        rows.append({"id": aid, "name": a.advertiser_name or aid, "bc": (bcs[a.owner_bc_id].name if a.owner_bc_id in bcs else ""),
                     "cells": cells, "blocked": blocked, "warnings": warns,
                     "tz": "UTC", "starts": acct_time.start_for(tz, start_back)[11:16],
                     "identity_pending": identity == "account" and not blocked, "terms_pending": terms_pending})
    if live:
        _live_recheck(db, models, rows, accounts, page_name, form_name)
    return {"rows": rows, "blocked": sum(1 for r in rows if r["blocked"]),
            "warned": sum(1 for r in rows if not r["blocked"] and r["warnings"]),
            "ready": sum(1 for r in rows if not r["blocked"]),
            "asset": {"page": page_name, "form": form_name}}


def sl_main_bc(db) -> str:
    try:
        from . import bc_assets
        return bc_assets.main_bc_id(db) or ""
    except Exception:      # noqa: BLE001
        return ""


LIVE_MAX = 15


def _live_recheck(db, models, rows: list, accounts: list, page_name: str, form_name: str) -> None:
    """Rows blocked only by a missing page / form: ask TikTok once (the same live re-read the launch
    does, which also refreshes the cache). At most LIVE_MAX accounts per call."""
    from .routes.campaigns import AssetResolveError, resolve_page_asset
    by_id = {a.advertiser_id: a for a in accounts}
    n = 0
    for r in rows:
        for key, kind, name in (("page", "instant_page", page_name), ("form", "lead_form", form_name)):
            c = r["cells"].get(key)
            if not name or not c or c["text"] != "missing" or n >= LIVE_MAX:
                continue
            n += 1
            try:
                got = resolve_page_asset(db, by_id[r["id"]], kind, name)
                r["cells"][key] = {**cell(OK, name), "id": got}
            except AssetResolveError as e:
                r["cells"][key] = cell(BAD, "missing", str(e)[:200] if "Couldn't read" in str(e) else c["hint"] + " (checked on TikTok just now)")
            except Exception as e:      # noqa: BLE001
                r["cells"][key] = cell(BAD, "missing", c["hint"] + f" (TikTok check failed: {str(e)[:80]})")
        r["blocked"], r["warnings"] = verdict(r["cells"])


# ---- TikTok's Lead Generation Terms (v155.13) --------------------------------------------------
TERMS_NOT = ("TikTok's Lead Generation Terms aren't confirmed for this ad account (API) — TikTok refuses Instant "
             "Form ads until they are. Confirm them with the button above.")


TERMS_AUTO = ("TikTok's Lead Generation Terms aren't confirmed for this ad account yet — the launch confirms them "
              "first (Settings › Launch › automatic confirmation is on), then creates the ad.")


def terms_auto_for(db, user_id) -> bool:
    """Whether this launcher switched on confirming the Terms at launch (v155.17)."""
    try:
        from . import settings_store
        return bool(settings_store.get_settings(db, user_id).get("lead_terms_auto"))
    except Exception:  # noqa: BLE001
        return False


def terms_cell(state, auto: bool = False) -> dict:
    """The review cell for lead_terms.status(): True / False / None (couldn't tell). With `auto`
    (the launcher confirms at launch) a missing confirmation is a note, not a block. Pure."""
    if state is True:
        return cell(OK, "accepted")
    if state is False and auto:
        return cell(WARN, "confirmed at launch", TERMS_AUTO)
    if state is False:
        return {**cell(BAD, "not accepted", TERMS_NOT), "terms": False}
    return cell(WARN, "not checked", "couldn't read the Lead Generation Terms state from TikTok — the launch will try")


# ---- identities (library / carousel ads publish under the account's own identity) ------------
_IDS: dict[str, tuple[float, list]] = {}
IDS_TTL = 600


def identities(db, acct) -> dict:
    """{state, text, hint} for the identity a library ad would use on this account. Cached 10 min."""
    hit = _IDS.get(acct.advertiser_id)
    if hit and time.time() - hit[0] < IDS_TTL:
        cands = hit[1]
    else:
        from .routes.campaigns import identity_candidates
        from . import tiktok_api
        try:
            cands = identity_candidates(db, acct)
        except tiktok_api.TikTokError as e:
            return cell(WARN, "couldn't read", f"couldn't read its identities: {e.message}"[:160])
        except Exception as e:      # noqa: BLE001
            return cell(WARN, "couldn't read", f"couldn't read its identities: {str(e)[:120]}")
        _IDS[acct.advertiser_id] = (time.time(), cands)
        if len(_IDS) > 2000:
            for k in sorted(_IDS, key=lambda k: _IDS[k][0])[:500]:
                _IDS.pop(k, None)
    if not cands:
        return cell(BAD, "none", "no TikTok identity on this account — connect a TikTok account to it or its Business Center")
    first = cands[0]
    name = first.get("_name") or ("BC profile" if first.get("identity_type") == "BC_AUTH_TT" else "TikTok account")
    more = f" (+{len(cands) - 1} fallback{'s' if len(cands) > 2 else ''})" if len(cands) > 1 else ""
    return cell(OK, name + more, "")
