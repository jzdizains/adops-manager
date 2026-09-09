"""/creators — which TikTok profiles (identities) each ad account can run ads as,
and the tools to fix "no identity owns the post": authorise a spark code on an
account / a Business Center / everywhere, and step-by-step guidance for linking a
creator's real profile (which only the creator can approve, in TikTok itself).
"""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import identities, jobs, models, queries
from ..database import get_db
from ..templating import render

router = APIRouter()

REPORT_TTL_H = 24


def scope_accounts(db: Session, scope: str) -> tuple[list[models.AdAccount], str]:
    """`all` | `bc:<bc id>` | `acct:<advertiser id>` → enabled accounts + a label."""
    accounts = queries.enabled_accounts(db)
    if scope.startswith("bc:"):
        bc_id = scope[3:]
        bc = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
        return [a for a in accounts if a.owner_bc_id == bc_id], f"BC {(bc.name if bc else '') or bc_id}"
    if scope.startswith("acct:"):
        adv = scope[5:]
        acct = next((a for a in accounts if a.advertiser_id == adv), None)
        return ([acct] if acct else []), ((acct.advertiser_name or adv) if acct else adv)
    return accounts, "every account"


def _fresh(raw: str) -> dict | None:
    from datetime import datetime, timedelta, timezone
    if not raw:
        return None
    try:
        rep = json.loads(raw)
        at = datetime.fromisoformat(rep.get("at", ""))
    except (ValueError, TypeError, AttributeError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return rep if datetime.now(timezone.utc) - at < timedelta(hours=REPORT_TTL_H) else None


@router.get("/creators")
def page(request: Request, db: Session = Depends(get_db)):
    accounts = queries.enabled_accounts(db)
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    recs = db.query(models.IdentityRecord).order_by(models.IdentityRecord.identity_type, models.IdentityRecord.display_name).all()
    by_acct: dict[str, list] = {}
    for r in recs:
        by_acct.setdefault(r.advertiser_id, []).append(r)
    qp = request.query_params
    want_bc = qp.get("bc", "")
    want_acct = qp.get("account", "")
    rows = []
    for a in accounts:
        if want_acct and a.advertiser_id != want_acct:
            continue
        if want_bc and a.owner_bc_id != want_bc:
            continue
        idents = by_acct.get(a.advertiser_id, [])
        bc = bcs.get(a.owner_bc_id or "")
        rows.append({"a": a, "bc": bc, "idents": idents, "fetched": idents[0].fetched_at if idents else None,
                     "kinds": {i.identity_type for i in idents}})
    # a spark code to authorise right away (arrived from a failed launch's "Fix this")
    spark = None
    sid = qp.get("spark", "")
    if sid.isdigit():
        spark = db.get(models.SparkCode, int(sid))
    creators = {}
    for r in recs:
        key = (r.identity_type, r.display_name or r.identity_id)
        creators[key] = creators.get(key, 0) + 1
    return render(request, "creators.html", {
        "title": "Creators", "rows": rows, "accounts": accounts, "bc_list": sorted(bcs.values(), key=lambda b: (b.name or b.bc_id).lower()),
        "type_labels": identities.TYPE_LABELS, "type_help": identities.TYPE_HELP,
        "n_with": sum(1 for r in rows if r["idents"]), "n_fetched": sum(1 for r in rows if r["fetched"]),
        "sync_report": _fresh(queries.get_setting(db, "identities_sync_report", "")),
        "auth_report": _fresh(queries.get_setting(db, "spark_authorize_report", "")),
        "sync_pending": jobs.pending(db, "identities_sync") is not None,
        "auth_pending": jobs.pending(db, "spark_authorize") is not None,
        "spark": spark, "want_acct": want_acct, "want_bc": want_bc, "fix": qp.get("fix", "") == "1",
        "ok": qp.get("ok", ""), "err": qp.get("err", ""),
    })


@router.post("/creators/sync")
def sync(scope: str = Form("all"), db: Session = Depends(get_db)):
    if not queries.any_access_token(db):
        return RedirectResponse("/creators?err=Connect+TikTok+first", status_code=303)
    accounts, label = scope_accounts(db, scope)
    if not accounts:
        return RedirectResponse("/creators?err=" + quote(f"No enabled ad account under {label}."), status_code=303)
    job, created = jobs.enqueue_once(db, "identities_sync", f"Refresh creator identities — {label}", {"scope": scope}, href="/creators")
    if not created:
        return RedirectResponse(f"/creators?ok=A+refresh+is+already+{job.status}+—+hold+on.", status_code=303)
    return RedirectResponse("/creators?ok=" + quote(f"Refreshing identities for {label} ({len(accounts)} account(s)) — the page updates when it's done."), status_code=303)


@router.post("/creators/authorize")
async def authorize(request: Request, db: Session = Depends(get_db)):
    """Authorise a spark code. One account → runs now and answers JSON (the pop-up
    shows the step-by-step result). A BC / every account → a job."""
    form = await request.form()
    scope = str(form.get("scope") or "all")
    code = str(form.get("code") or "").strip()
    sid = str(form.get("spark_code_id") or "")
    if not code and sid.isdigit():
        sc = db.get(models.SparkCode, int(sid))
        code = (sc.code if sc else "").strip()
    if not code:
        return JSONResponse({"ok": False, "message": "Paste the spark code (or pick one) first."}, status_code=400)
    accounts, label = scope_accounts(db, scope)
    if not accounts:
        return JSONResponse({"ok": False, "message": f"No enabled ad account under {label}."}, status_code=400)
    # remember the code in the Sparks list so it can be launched with afterwards
    if not db.query(models.SparkCode).filter_by(code=code).first():
        db.add(models.SparkCode(name=str(form.get("name") or "").strip()[:120] or code[:14], code=code,
                                media_type="CAROUSEL" if str(form.get("media_type") or "").upper() == "CAROUSEL" else "VIDEO"))
        db.commit()
    if len(accounts) == 1:
        from starlette.concurrency import run_in_threadpool
        res = await run_in_threadpool(identities.authorize, db, accounts[0], code)
        res["account"] = accounts[0].advertiser_name or accounts[0].advertiser_id
        res["advertiser_id"] = accounts[0].advertiser_id
        sc = db.query(models.SparkCode).filter_by(code=code).first()
        res["spark_id"] = sc.id if sc else None
        return JSONResponse(res)
    job, created = jobs.enqueue_once(db, "spark_authorize", f"Authorise spark code — {label}", {"scope": scope, "code": code}, href="/creators")
    return JSONResponse({"ok": True, "queued": True, "job_id": job.id, "message": (f"Authorising on {len(accounts)} account(s) in the background — "
                                                                                      "the page refreshes with the result.") if created else f"Already {job.status} — hold on."})


@router.post("/creators/report/dismiss")
def dismiss_report(which: str = Form("sync"), db: Session = Depends(get_db)):
    queries.set_setting(db, "spark_authorize_report" if which == "auth" else "identities_sync_report", "")
    return RedirectResponse("/creators", status_code=303)
