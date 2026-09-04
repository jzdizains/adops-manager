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
    r = issues.scan(db)
    ads = db.query(models.Issue).filter_by(category="ad").count()
    return {"ok": True, "detail": f"scanned {r['accounts_scanned']} account(s): {r['issues']} issue(s), {ads} rejected ad(s)"}


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
    found, failed = pixels.sync_pixels_inventory(db)
    return {"ok": failed == 0, "detail": f"{found} pixel(s) synced" + (f", {failed} account(s) failed" if failed else ""), "href": "/pixels"}


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
