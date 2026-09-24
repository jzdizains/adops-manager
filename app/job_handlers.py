"""Job handlers — the work behind every background action. Each returns
{ok, detail, href?}; the notification shows `detail` and opens `href`."""
from __future__ import annotations

from sqlalchemy.orm import Session

import json

from . import jobs, models, queries


def _job_user_id(p: dict) -> int | None:
    """The workspace a job belongs to, from its payload. None (older jobs, or the
    super-admin's 'Everyone' view) keeps the original global, every-account behaviour."""
    v = p.get("user_id")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@jobs.handler("launch")
def _launch(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import campaigns as engine
    ids = [str(x) for x in p.get("advertiser_ids") or []]
    by_id = {a.advertiser_id: a for a in db.query(models.AdAccount)
             .filter(models.AdAccount.advertiser_id.in_(ids)).all()} if ids else {}
    accounts = [by_id[i] for i in ids if i in by_id]
    fields = p.get("fields") or {}
    ref = p.get("batch_ref") or None

    def prog(i, n):
        jobs.progress(db, job, f"{i} of {n}")

    lib_pairs = [(by_id[str(a)], cid) for a, cid in (p.get("pairs") or []) if str(a) in by_id]
    spark_pairs = [(by_id[str(a)], sid) for a, sid in (p.get("spark_pairs") or []) if str(a) in by_id]
    if lib_pairs or spark_pairs:
        # a board launch (v123) can carry both: library creatives on some accounts, spark
        # posts on the others — one batch, one result page, one progress count
        total = len(lib_pairs) + len(spark_pairs)
        ref = ref or engine.error_messages.new_ref()
        if lib_pairs:
            ref = engine.run_batch_assigned(db, lib_pairs, fields, batch_ref=ref, on_progress=lambda i, n: prog(i, total))
        if spark_pairs:
            ref = engine.run_batch_assigned_sparks(db, spark_pairs, fields, batch_ref=ref,
                                                   on_progress=lambda i, n: prog(len(lib_pairs) + i, total))
        if lib_pairs and spark_pairs:
            # each runner remembers its own recipe; a mixed batch needs both per-account maps
            # so "Retry failed" relaunches the same creative or post on the same account
            engine._remember_batch(db, ref, {**fields,
                                             "_creative_by_account": {str(a.advertiser_id): int(cid) for a, cid in lib_pairs if cid is not None},
                                             "_spark_by_account": {str(a.advertiser_id): int(sid) for a, sid in spark_pairs if sid is not None}})
    else:
        if not accounts:
            return {"ok": False, "detail": "no matching accounts to launch"}
        ref = engine.run_batch(db, accounts, fields, batch_ref=ref, on_progress=prog)
    logs = db.query(models.LaunchLog).filter_by(batch_ref=ref).all()
    ok = sum(1 for l in logs if l.ok)
    fail = len(logs) - ok
    return {"ok": fail == 0 and ok > 0,
            "detail": f"{ok} launched" + (f", {fail} failed" if fail else "") + " — open the result",
            "href": f"/campaigns/result/{ref}"}


@jobs.handler("bid")
def _bid(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import campaigns as engine
    r = engine.apply_bid(db, str(p["advertiser_id"]), str(p["campaign_id"]), float(p["cap"]))
    if r.get("error") and not r.get("applied"):
        return {"ok": False, "detail": r["error"]}
    d = f"cost cap ${r['cap']:.2f} set on {r['applied']} of {r['n']} ad group(s)"
    if r.get("errors"):
        d += " — " + " · ".join(r["errors"])[:300]
    return {"ok": bool(r.get("ok")), "detail": d, "href": "/status"}


@jobs.handler("bid_bump")
def _bid_bump(db: Session, p: dict, job: models.Job) -> dict:
    from . import bid_bump
    r = bid_bump.run(db, [int(x) for x in (p.get("watch_ids") or [])], p.get("user_id"),
                     should_stop=lambda: jobs.should_stop(db, job))
    d = f"+${r['step']:.2f} on {r['bumped']} ad group(s)"
    if r["skipped"]:
        d += f", {r['skipped']} no longer due"
    if r["failed"]:
        d += " — " + " · ".join(r["failed"])[:300]
    return {"ok": not r["failed"], "detail": d, "href": "/monitor?view=automation"}


def _n_failed(r: dict) -> int:
    """Failed accounts: the no-access ones are folded into ONE summary line (v155.1)."""
    na = int(r.get("no_access") or 0)
    return len(r["failed"]) - (1 if na else 0) + na


@jobs.handler("instant_page_clone_all")
def _instant_page_clone_all(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import instant_pages
    r = instant_pages.clone_to_many(db, str(p["page_id"]), str(p["from_advertiser_id"]), [str(t) for t in (p.get("targets") or [])],
                                    should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t),
                                    name=str(p.get("name") or ""), new_url=str(p.get("new_url") or ""), new_text=str(p.get("new_text") or ""))
    d = f"“{p.get('name', '')}” cloned to {len(r['ok'])} account(s)"
    if r["failed"]:
        d += f", {_n_failed(r)} failed — " + " · ".join(r["failed"])[:900]
    if r.get("notes"):
        d += " · " + " · ".join(r["notes"])[:200]
    if r["stopped"]:
        d += " (stopped)"
    return {"ok": not r["failed"] and not r["stopped"], "detail": d, "href": "/instant-pages"}


@jobs.handler("lead_form_clone_all")
def _lead_form_clone_all(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import lead_forms
    r = lead_forms.clone_to_many(db, str(p["form_id"]), str(p["from_advertiser_id"]), str(p.get("name") or ""),
                                 [str(t) for t in (p.get("targets") or [])],
                                 should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t))
    d = f"form “{p.get('name', '')}” now on {len(r['ok'])} more account(s)"
    if r["failed"]:
        d += f", {_n_failed(r)} failed — " + " · ".join(r["failed"])[:900]
    if r["stopped"]:
        d += " (stopped)"
    return {"ok": not r["failed"] and not r["stopped"], "detail": d, "href": "/lead-forms"}


@jobs.handler("lead_form_build_many")
def _lead_form_build_many(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import lead_forms
    r = lead_forms.build_on_many(db, str(p["template_form_id"]), str(p["from_advertiser_id"]), str(p.get("name") or ""),
                                 dict(p.get("edits") or {}), [str(t) for t in (p.get("targets") or [])],
                                 should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t))
    d = f"form “{p.get('name', '')}” built on {len(r['ok'])} account(s)"
    if r["failed"]:
        d += f", {_n_failed(r)} failed — " + " · ".join(r["failed"])[:900]
    if r["stopped"]:
        d += " (stopped)"
    return {"ok": not r["failed"] and not r["stopped"], "detail": d, "href": "/lead-forms"}


@jobs.handler("lead_terms_accept")
def _lead_terms_accept(db: Session, p: dict, job: models.Job) -> dict:
    """The operator's "Accept Lead Generation Terms" for many accounts (v155.13), one at a time."""
    from . import lead_terms
    ids = [str(a) for a in (p.get("advertiser_ids") or [])]
    names = {a.advertiser_id: (a.advertiser_name or a.advertiser_id)
             for a in db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(ids or [""]))}
    ok, failed = [], []
    for i, a in enumerate(ids):
        if jobs.should_stop(db, job):
            break
        jobs.progress(db, job, f"{i + 1} of {len(ids)} — {names.get(a, a)}")
        good, msg = lead_terms.accept(db, a)
        (ok.append(a) if good else failed.append(f"{names.get(a, a)}: {msg}"))
    d = f"Lead Generation Terms accepted on {len(ok)} of {len(ids)} account(s)"
    if failed:
        d += " — " + " · ".join(failed)[:500]
    return {"ok": not failed, "detail": d, "href": "/jobs"}


@jobs.handler("instant_page_build")
def _instant_page_build(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import instant_pages
    r = instant_pages.build_on_accounts(db, int(p["template_id"]), [str(t) for t in (p.get("targets") or [])],
                                        should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t))
    d = f"page “{r.get('name', '')}” built on {len(r['ok'])} account(s)"
    if r["failed"]:
        d += f", {len(r['failed'])} failed — " + " · ".join(r["failed"])[:500]
    if r["stopped"]:
        d += " (stopped)"
    if r.get("shots"):
        d += " · screenshots: " + " ".join(f"/instant-pages/builds/{s}" for s in r["shots"][-3:])
    return {"ok": not r["failed"] and not r["stopped"], "detail": d[:900], "href": "/instant-pages"}


@jobs.handler("campaign_edit")
def _edit(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import campaigns as engine
    changed, errors = engine.apply_edit(db, str(p["advertiser_id"]), str(p["campaign_id"]),
                                        p.get("campaign_name", ""), p.get("campaign_budget", ""),
                                        p.get("adgroup_budget_all", ""), p.get("cost_cap_all", ""))
    detail = "; ".join(changed) or "no changes applied"
    if errors:
        detail += " — " + "; ".join(errors)
    return {"ok": bool(changed) and not errors, "detail": detail[:600]}


@jobs.handler("status_sync")
def _status_sync(db: Session, p: dict, job: models.Job) -> dict:
    from . import live_spend
    r = live_spend.sync_campaigns(db)
    errs = r.get("errors") or []
    return {"ok": not errs, "detail": f"synced {r.get('synced', 0)} account(s)" + (f", {len(errs)} failed" if errs else ""),
            "href": "/status"}


@jobs.handler("issues_scan")
def _issues_scan(db: Session, p: dict, job: models.Job) -> dict:
    from . import issues
    r = issues.scan(db, should_stop=lambda: jobs.should_stop(db, job),
                    on_progress=lambda t: jobs.progress(db, job, t))
    failed = r.get("accounts_failed", 0)
    if r.get("stopped"):
        return {"ok": False, "detail": f"stopped by you after {r.get('accounts_ok', 0)} account(s) "
                                       f"({r.get('ads_read', 0)} ads read, {r.get('rejected_found', 0)} rejected) — "
                                       "the rest was not scanned"}
    ads = db.query(models.Issue).filter_by(category="ad").count()
    return {"ok": failed == 0,
            "detail": f"read {r.get('ads_read', 0)} ad(s) across {r.get('accounts_ok', 0)} account(s): "
                      f"{ads} rejected, {r['issues']} issue(s) in total"
                      + (f" — {failed} account(s) could not be read, see the Appeals page" if failed else "")}


@jobs.handler("appeals_file")
def _appeals_file(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import appeals_page
    ids = [int(x) for x in p.get("ids") or []]
    r = appeals_page.file_rows(db, ids, str(p.get("reason") or ""))
    return {"ok": r["ok"], "detail": r["detail"], "href": "/appeals"}


@jobs.handler("appeals_refresh")
def _appeals_refresh(db: Session, p: dict, job: models.Job) -> dict:
    from . import appeals
    n = appeals.refresh(db, max_age_min=0)
    waiting = db.query(models.Appeal).filter(models.Appeal.status == "appealing").count()
    return {"ok": True, "detail": f"{n} appeal(s) answered, {waiting} still waiting", "href": "/appeals"}


@jobs.handler("invite_autoaccept")
def _invite_autoaccept(db: Session, p: dict, job: models.Job) -> dict:
    from . import invite_autoaccept
    rec = db.get(models.InviteAccept, int(p.get("record_id") or 0))
    if not rec:
        return {"ok": False, "detail": "auto-accept record not found"}
    return invite_autoaccept.run(db, rec)


@jobs.handler("partner_setup")
def _partner_setup(db: Session, p: dict, job: models.Job) -> dict:
    from . import partners, queries
    row = db.get(models.PartnerSetup, int(p["row_id"]))
    token = queries.any_access_token(db)
    if not row or not token:
        return {"ok": False, "detail": "setup not found or TikTok not connected"}
    partners.run(db, row, token)
    steps = []
    for label, st, err in (("partner", row.partner_status, row.partner_error),
                           ("invite", row.invite_status, row.invite_error),
                           ("TikTok account", row.assign_status, row.assign_error)):
        if st in (partners.DONE,):
            steps.append(f"{label} ✓")
        elif st == partners.WAITING:
            steps.append(f"{label}: waiting for the invite")
        elif st == partners.ERROR:
            steps.append(f"{label} ✕ {err}")
    bad = partners.ERROR in (row.partner_status, row.invite_status, row.assign_status)
    return {"ok": not bad, "detail": "; ".join(steps) or "nothing to do", "href": f"/partners#setup-{row.id}"}


@jobs.handler("source_fix")
def _source_fix(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import campaigns as engine
    changes, errors = engine.fix_sources(db, bool(p.get("all")), str(p.get("advertiser_id") or ""),
                                         str(p.get("campaign_id") or ""))
    if not changes and not errors:
        return {"ok": True, "detail": "nothing to fix"}
    d = "; ".join(changes)[:300]
    if errors:
        d += (" — " if d else "") + "; ".join(errors)[:300]
    return {"ok": not errors, "detail": d, "href": "/campaigns/source-check"}


@jobs.handler("pixels_sync")
def _pixels_sync(db: Session, p: dict, job: models.Job) -> dict:
    from .routes import pixels
    rep = pixels.sync_pixels_inventory(db, str(p.get("scope") or "all"))
    n_fail = len(rep["failures"])
    detail = f"{rep['pixels']} pixel(s) across {rep['ok_accounts']} of {rep['accounts']} account(s) ({rep['label']})"
    if n_fail:
        f0 = rep["failures"][0]
        detail += f" — {n_fail} account(s) failed: {f0['account']}: {f0['friendly']}"
        if n_fail > 1:
            detail += " (see the Pixels page for all of them)"
    if rep["accounts"] == 0:
        return {"ok": False, "detail": "no enabled ad accounts to sync — connect TikTok first", "href": "/pixels"}
    # partial failures are not a failed sync: the pixels that came back are in the list
    return {"ok": rep["ok_accounts"] > 0, "detail": detail, "href": "/pixels"}


@jobs.handler("identities_sync")
def _identities_sync(db: Session, p: dict, job: models.Job) -> dict:
    import json as _json
    from . import identities, queries
    from .routes.creators import scope_accounts
    accounts, label = scope_accounts(db, str(p.get("scope") or "all"))
    rep = identities.sync(db, accounts)
    rep["scope"], rep["label"] = str(p.get("scope") or "all"), label
    queries.set_setting(db, "identities_sync_report", _json.dumps(rep))
    n_fail = len(rep["failures"])
    detail = f"{rep['identities']} identit{'y' if rep['identities'] == 1 else 'ies'} across {rep['ok_accounts']} of {rep['accounts']} account(s) ({label})"
    if n_fail:
        f0 = rep["failures"][0]
        detail += f" — {n_fail} account(s) failed: {f0['account']}: {f0['friendly']}"
    if not accounts:
        return {"ok": False, "detail": "no enabled ad accounts — connect TikTok first", "href": "/creators"}
    return {"ok": rep["ok_accounts"] > 0, "detail": detail, "href": "/creators"}


@jobs.handler("spark_authorize")
def _spark_authorize(db: Session, p: dict, job: models.Job) -> dict:
    """Authorise one spark code on every account in the scope (a BC, or all)."""
    import json as _json
    from datetime import datetime, timezone
    from . import identities, queries
    from .routes.creators import scope_accounts
    accounts, label = scope_accounts(db, str(p.get("scope") or "all"))
    code = str(p.get("code") or "")
    results = []
    for i, acct in enumerate(accounts):
        jobs.progress(db, job, f"{i + 1} of {len(accounts)}")
        r = identities.authorize(db, acct, code)
        results.append({"account": acct.advertiser_name or acct.advertiser_id, "advertiser_id": acct.advertiser_id, "ok": r["ok"],
                        "message": r["message"], "identity": r.get("identity"), "item_id": r.get("item_id", "")})
    ok = sum(1 for r in results if r["ok"])
    queries.set_setting(db, "spark_authorize_report", _json.dumps({"at": datetime.now(timezone.utc).isoformat(), "code": code[:12] + "…", "code_full": code, "label": label,
                                                                    "ok": ok, "total": len(results), "results": results[:80]}))
    return {"ok": ok > 0 and ok == len(results), "detail": f"spark code usable on {ok} of {len(results)} account(s) ({label})"
            + ("" if ok == len(results) else " — see the Creators page for the ones that need the creator linked"), "href": "/creators"}


@jobs.handler("pixel_link_all")
def _pixel_link_all(db: Session, p: dict, job: models.Job) -> dict:
    from . import queries
    from .routes import pixels
    rec = db.get(models.PixelRecord, int(p["record_id"]))
    token = queries.any_access_token(db)
    if not rec or not token or not rec.owner_bc_id:
        return {"ok": False, "detail": "pixel not found, not BC-owned, or TikTok not connected"}
    only = p.get("only") or None
    ok_count, failed = pixels._link_pixel_to_bc_accounts(db, token, rec.owner_bc_id, rec.pixel_code or rec.pixel_id, only=only)
    return {"ok": not failed, "detail": f"linked to {ok_count} account(s)" + (f", failed: {' '.join(failed[:8])}" if failed else ""), "href": "/pixels"}


@jobs.handler("adgroup_duplicate")
def _adgroup_duplicate(db: Session, p: dict, job: models.Job) -> dict:
    """Duplicate one ad group, with its ads, inside the same campaign."""
    from . import adgroup_copy
    acct = (db.query(models.AdAccount)
            .filter_by(advertiser_id=str(p.get("advertiser_id") or "")).first())
    if acct is None or not acct.access_token:
        return {"ok": False, "detail": "that ad account is not connected", "href": "/status"}
    rep = adgroup_copy.duplicate(db, acct, str(p.get("campaign_id") or ""),
                                 str(p.get("adgroup_id") or ""), int(p.get("copies") or 1),
                                 on_progress=lambda t: jobs.progress(db, job, t))
    queries.set_setting(db, "adgroup_duplicate_last", json.dumps(rep)[:200_000])
    db.commit()
    if rep.get("error"):
        return {"ok": False, "detail": rep["error"], "href": "/status"}
    return {"ok": not rep.get("errors"), "detail": rep.get("summary", ""), "href": "/status"}


@jobs.handler("bc_assets_scan")
def _bc_assets_scan(db: Session, p: dict, job: models.Job) -> dict:
    """Read-only: which ad accounts have the pixel and the profiles (see bc_assets)."""
    from . import bc_assets
    uid = _job_user_id(p)      # whose workspace this audit is for (None = the global/Everyone view)
    snap = bc_assets.scan(db, on_progress=lambda t: jobs.progress(db, job, t),
                          should_stop=lambda: jobs.should_stop(db, job), user_id=uid)
    s = snap.get("summary") or {}
    detail = (f"{s.get('ready', 0)} of {s.get('accounts', 0)} account(s) fully wired · "
              f"{s.get('in_main_bc', 0)} in the main BC · {s.get('with_pixel', 0)} with a pixel"
              if s else "nothing read")
    if snap.get("errors"):
        detail += f" · {len(snap['errors'])} note(s)"
    return {"ok": not snap.get("errors"), "detail": detail, "href": "/bc-assets"}


@jobs.handler("bc_assets_wire")
def _bc_assets_wire(db: Session, p: dict, job: models.Job) -> dict:
    """One ad account: share it into the main BC, link the pixel, link every profile."""
    from . import bc_assets
    rep = bc_assets.wire(db, str(p.get("advertiser_id") or ""), role=str(p.get("role") or "OPERATOR"),
                         dry_run=bool(p.get("dry_run", True)),
                         on_progress=lambda t: jobs.progress(db, job, t), user_id=_job_user_id(p))
    if rep.get("error"):
        return {"ok": False, "detail": rep["error"], "href": "/bc-assets"}
    bad = [s for s in rep.get("steps", []) if s.get("ok") is False]
    return {"ok": not bad, "detail": rep.get("summary", ""), "href": "/bc-assets"}


@jobs.handler("bc_assets_connect")
def _bc_assets_connect(db: Session, p: dict, job: models.Job) -> dict:
    """A whole Business Center: share its ad accounts into the main BC, then link everything."""
    from . import bc_assets
    rep = bc_assets.connect_bc(db, str(p.get("bc_id") or ""), role=str(p.get("role") or "OPERATOR"),
                               dry_run=bool(p.get("dry_run", True)), email=str(p.get("email") or ""),
                               on_progress=lambda t: jobs.progress(db, job, t), user_id=_job_user_id(p))
    if rep.get("error"):
        return {"ok": False, "detail": rep["error"], "href": "/bc-assets"}
    bad = [s for s in rep.get("steps", []) if s.get("ok") is False]
    return {"ok": not bad, "detail": rep.get("summary", ""), "href": "/bc-assets"}


@jobs.handler("audience_sync")
def _audience_sync(db: Session, p: dict, job: models.Job) -> dict:
    from . import audience
    days = p.get("days") or None
    r = audience.sync(db, days, should_stop=lambda: jobs.should_stop(db, job),
                      on_progress=lambda t: jobs.progress(db, job, t), hot_only=bool(p.get("hot_only")))
    if r["stopped"]:
        return {"ok": False, "detail": f"stopped by you after {r['ok'] + r['failed']} of {r['accounts']} account(s) "
                                       f"({r['rows']} rows stored)", "href": "/audience"}
    return {"ok": r["failed"] == 0,
            "detail": f"{r['rows']} breakdown rows from {r['ok']} account(s) in {r['calls']} calls"
                      + (f" (resumed — {r['resumed']} already done before a restart)" if r.get("resumed") else "")
                      + (f" — {r['failed']} account(s) failed: " + "; ".join(e["name"] + ": " + e["error"][:60] for e in r["errors"][:3]) if r["failed"] else ""),
            "href": "/audience"}


@jobs.handler("music_sync")
def _music_sync(db: Session, p: dict, job: models.Job) -> dict:
    from . import music_library
    r = music_library.sync(db, should_stop=lambda: jobs.should_stop(db, job),
                           on_progress=lambda t: jobs.progress(db, job, t))
    detail = f"{r['carousel_ok']} of {r['tracks']} library tracks usable in carousels ({r['pages']} page(s) read)"
    if r["stopped"]:
        return {"ok": False, "detail": "stopped by you — " + detail, "href": "/creatives?view=carousels"}
    if r["errors"]:
        detail += " — " + "; ".join(e[:80] for e in r["errors"][:2])
    return {"ok": not r["errors"], "detail": detail, "href": "/creatives?view=carousels"}


# ---------------------------------------------------------------------------
# v147 stocking systems (Phase 3)
# ---------------------------------------------------------------------------

@jobs.handler("spark_check")
def _spark_check(db: Session, p: dict, job: models.Job) -> dict:
    """New spark codes checked on TikTok (spark_check.py) — the retries come from the sweep."""
    from . import spark_check, tiktok_api
    r = spark_check.run(db, models, tiktok_api, ids=[int(i) for i in p.get("ids") or []], limit=500)
    d = f"{r.get('ok', 0)} ok"
    if r.get("bad"):
        d += f", {r['bad']} rejected by TikTok"
    if r.get("checking"):
        d += f", {r['checking']} retrying"
    if r.get("error"):
        d += f", {r['error']} couldn't be checked"
    return {"ok": not r.get("bad") and not r.get("error"), "detail": d, "href": "/spark-codes"}


@jobs.handler("post_thumbs")
def _post_thumbs(db: Session, p: dict, job: models.Job) -> dict:
    """Copy a Business Center's post covers while TikTok's URLs are alive (thumbs.py)."""
    from . import profile_videos
    r = profile_videos.cache_covers(db, str(p.get("bc_id") or ""))
    return {"ok": True, "detail": f"{r['saved']} cover(s) saved" + (f", {r['failed']} already expired" if r["failed"] else "")}


@jobs.handler("profile_refresh")
def _profile_refresh(db: Session, p: dict, job: models.Job) -> dict:
    """Re-read one BC's profiles in the background (a stale stored copy was served)."""
    from . import profile_videos, scope as scope_mod
    uid = p.get("user_id")
    sc = scope_mod.Scope(mode="user" if uid is not None else "all",
                         ids=scope_mod.owned_ids(db, uid) if uid is not None else None, user_id=uid)
    r = profile_videos.list_for_bc(db, sc, str(p.get("bc_id") or ""), refresh=True)
    return {"ok": bool(r.get("ok")), "detail": f"{r.get('total', 0)} post(s)" if r.get("ok") else (r.get("error") or "TikTok didn't answer")}


@jobs.handler("card_push")
def _card_push(db: Session, p: dict, job: models.Job) -> dict:
    """Put one display card on every account of its workspace (display_cards.push)."""
    from . import display_cards as DC
    card = db.get(models.DisplayCard, int(p.get("card_id") or 0))
    if card is None:
        return {"ok": False, "detail": "the display card was deleted"}
    r = DC.push(db, card, DC.push_targets(db, card.owner_user_id),
                should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t))
    d = f"“{card.name}”: {r['done']} account(s) added, {r['skipped']} already had it"
    if r["failed"]:
        d += f", {len(r['failed'])} failed — " + ", ".join(r["failed"][:8])
    return {"ok": not r["failed"], "detail": d, "href": "/presets"}


@jobs.handler("video_caption")
def _video_caption(db: Session, p: dict, job: models.Job) -> dict:
    """Burn one queued caption into its video copy (video_caption.py)."""
    from . import video_caption
    return video_caption.process(db, models, int(p.get("creative_id") or 0))


@jobs.handler("asset_sync")
def _asset_sync(db: Session, p: dict, job: models.Job) -> dict:
    """Instant Pages / Lead Forms re-read for the accounts that were in view (v151 audit)."""
    from .routes import instant_pages
    kind = "form" if p.get("kind") == "form" else "page"
    r = instant_pages.sync_many(db, kind, [str(x) for x in p.get("advertiser_ids") or []],
                                should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t))
    what = "instant page(s)" if kind == "page" else "lead form(s)"
    d = f"{r['total']} {what} across {r['ok']} of {r['accounts']} account(s)"
    if r["failed"]:
        d += f" — {len(r['failed'])} couldn't be read: " + "; ".join(r["failed"][:3])
    return {"ok": r["ok"] > 0 or not r["accounts"], "detail": d, "href": "/instant-pages" if kind == "page" else "/lead-forms"}


@jobs.handler("asset_builds")
def _asset_builds(db: Session, p: dict, job: models.Job) -> dict:
    """Work through the Instant Page / Form build queue (asset_builds.py)."""
    from . import asset_builds
    r = asset_builds.run(db, models, should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t))
    return {"ok": r["ok"] == r["done"], "detail": f"{r['ok']} of {r['done']} built", "href": "/instant-pages"}


@jobs.handler("page_stock")
def _page_stock(db: Session, p: dict, job: models.Job) -> dict:
    """One stocking pass: missing Instant Pages copied onto the workspace's accounts."""
    from . import page_stock
    r = page_stock.run(db, models, p.get("user_id"), force=bool(p.get("force")),
                       should_stop=lambda: jobs.should_stop(db, job), on_progress=lambda t: jobs.progress(db, job, t))
    if r.get("reason") and not r.get("made") and not r.get("failed"):
        return {"ok": r["reason"] == "off", "detail": r["reason"], "href": "/instant-pages"}
    d = f"{r.get('made', 0)} page(s) copied" + (f", {r['failed']} failed" if r.get("failed") else "")
    if r.get("remaining"):
        d += f", {r['remaining']} still to go (next run in 15 min)"
    if r.get("reason"):
        d += f" — {r['reason']}"
    return {"ok": not r.get("failed"), "detail": d, "href": "/instant-pages"}
