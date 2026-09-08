"""Job handlers — the work behind every background action. Each returns
{ok, detail, href?}; the notification shows `detail` and opens `href`."""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import jobs, models


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

    if p.get("pairs"):
        pairs = [(by_id[str(a)], cid) for a, cid in p["pairs"] if str(a) in by_id]
        ref = engine.run_batch_assigned(db, pairs, fields, batch_ref=ref, on_progress=prog)
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


@jobs.handler("pixel_link_all")
def _pixel_link_all(db: Session, p: dict, job: models.Job) -> dict:
    from . import queries
    from .routes import pixels
    rec = db.get(models.PixelRecord, int(p["record_id"]))
    token = queries.any_access_token(db)
    if not rec or not token or not rec.owner_bc_id:
        return {"ok": False, "detail": "pixel not found, not BC-owned, or TikTok not connected"}
    ok_count, failed = pixels._link_pixel_to_bc_accounts(db, token, rec.owner_bc_id, rec.pixel_id)
    return {"ok": not failed, "detail": f"linked to {ok_count} account(s)" + (f", failed: {' '.join(failed[:8])}" if failed else ""), "href": "/pixels"}


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
