"""/appeals — every TikTok ad-review rejection and what happened to its appeal.

Automatic filing lives in app/appeals.py (runs inside the issue scan). This
page is the operator's view + manual controls: appeal one, appeal all open,
check TikTok for answers, dismiss a rejection that was fixed another way."""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import appeals as appeals_mod
from .. import issues as issues_mod
from .. import models
from ..database import get_db
from ..settings_store import get_settings
from ..templating import render

router = APIRouter()


def _token_for(db: Session, advertiser_id: str) -> str:
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    return (acct.access_token if acct else "") or ""


OPEN_STATUSES = ("pending", "skipped", "error")    # rejected, no appeal on file → the operator can act
NO_BC = "none"                                      # filter value: accounts TikTok lists under no Business Center


def _back(ok: str = "", err: str = "", bc: str = "", camp: str = "") -> RedirectResponse:
    """Back to the page, keeping the Business Center / campaign filter the
    operator had set."""
    parts = []
    if ok:
        parts.append("ok=" + quote(ok))
    elif err:
        parts.append("err=" + quote(err))
    if bc:
        parts.append("bc=" + quote(bc))
    if camp:
        parts.append("camp=" + quote(camp))
    return RedirectResponse("/appeals" + ("?" + "&".join(parts) if parts else ""), status_code=303)


def _bc_of_accounts(db: Session) -> tuple[dict[str, str], dict[str, str]]:
    """advertiser_id → owner bc_id (may be ""), and bc_id → display name."""
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    bc_of = {a.advertiser_id: (a.owner_bc_id or "")
             for a in db.query(models.AdAccount.advertiser_id, models.AdAccount.owner_bc_id).all()}
    return bc_of, bc_names


def _passes(row, bc: str, camp: str, bc_of: dict[str, str]) -> bool:
    if bc:
        acct_bc = bc_of.get(row.advertiser_id, "")
        if bc == NO_BC:
            if acct_bc:
                return False
        elif acct_bc != bc:
            return False
    if camp and camp.lower() not in (row.campaign_name or row.campaign_id or "").lower():
        return False
    return True


def _group_key(row) -> str:
    """Rows are grouped by campaign NAME: identical campaigns launched across
    many accounts share a name (it is also the P&L source), so one group is
    the whole batch, not one account's copy of it."""
    return row.campaign_name or row.campaign_id or ""


def _open_rows_for(db: Session, campaign: str, bc: str, camp: str) -> list:
    """The open rejections in one campaign group under the current filter —
    exactly the rows the group's “Appeal all” button shows."""
    bc_of, _ = _bc_of_accounts(db)
    rows = (db.query(models.Appeal).filter(models.Appeal.status.in_(OPEN_STATUSES))
            .order_by(models.Appeal.created_at.desc()).all())
    return [r for r in rows if _group_key(r) == campaign and _passes(r, bc, camp, bc_of)]


@router.get("/appeals")
def appeals_page(request: Request, bc: str = "", camp: str = "", db: Session = Depends(get_db)):
    s = get_settings(db)
    bc, camp = bc.strip(), camp.strip()
    bc_of, bc_names = _bc_of_accounts(db)
    rows = db.query(models.Appeal).order_by(models.Appeal.created_at.desc()).limit(500).all()
    all_open = [r for r in rows if r.status in OPEN_STATUSES]
    # BC picker: EVERY Business Center the login sees (0 open included), each
    # with its open-rejection count before the filter; "No Business Center"
    # only when some ad account isn't listed under a BC.
    bc_counts: dict[str, int] = {}
    for r in all_open:
        k = bc_of.get(r.advertiser_id, "") or NO_BC
        bc_counts[k] = bc_counts.get(k, 0) + 1
    bc_options = sorted(((k, name, bc_counts.get(k, 0)) for k, name in bc_names.items()),
                        key=lambda t: t[1].lower())
    if any(not v for v in bc_of.values()) or NO_BC in bc_counts:
        bc_options.append((NO_BC, "No Business Center", bc_counts.get(NO_BC, 0)))
    if bc and bc != NO_BC and bc not in bc_names:
        bc = ""            # stale link to a BC that is gone → show everything
    vis = [r for r in rows if _passes(r, bc, camp, bc_of)]
    open_rows = [r for r in vis if r.status in OPEN_STATUSES]
    waiting = [r for r in vis if r.status == "appealing"]
    history = [r for r in vis if r.status in appeals_mod.FINAL]
    preview = appeals_mod.render_reason(
        s.get("appeal_reason", ""), ad_name="My ad", campaign_name="MyCampaign_1",
        reasons="The ad or video has no background audio")
    # when each campaign was created (TikTok create_time, synced into CampaignRecord)
    cids = {r.campaign_id for r in rows if r.campaign_id}
    created = {}
    if cids:
        for rec in db.query(models.CampaignRecord).filter(models.CampaignRecord.campaign_id.in_(cids)).all():
            if rec.launched_at:
                created[rec.campaign_id] = rec.launched_at
    # open rejections grouped by campaign (newest group first, order of first appearance)
    groups: dict[str, dict] = {}
    for r in open_rows:
        g = groups.setdefault(_group_key(r), {"key": _group_key(r), "rows": [], "accounts": set(),
                                              "bcs": set(), "created": None})
        g["rows"].append(r)
        g["accounts"].add(r.advertiser_id)
        b = bc_of.get(r.advertiser_id, "")
        g["bcs"].add(bc_names.get(b, b) if b else "no BC")
        c = created.get(r.campaign_id)
        if c and (g["created"] is None or c < g["created"]):
            g["created"] = c
    for g in groups.values():
        g["n_accounts"] = len(g["accounts"])
        g["bc_label"] = ", ".join(sorted(g["bcs"]))
    # what the last scan actually saw (so "0 open" is explainable)
    last_run = db.query(models.ScanRun).order_by(models.ScanRun.id.desc()).first()
    run_counts: list[tuple[str, int]] = []
    run_errors: list[dict] = []
    if last_run:
        try:
            counts = json.loads(last_run.status_counts or "{}")
            run_counts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        except ValueError:
            run_counts = []
        try:
            run_errors = json.loads(last_run.errors or "[]")
        except ValueError:
            run_errors = []
    return render(request, "appeals.html", {
        "title": "Appeals", "s": s, "summary": appeals_mod.summary(db), "created": created,
        "last_run": last_run, "run_counts": run_counts, "run_errors": run_errors,
        "bad_tokens": issues_mod.BAD_STATUS_TOKENS,
        "open_rows": open_rows, "waiting": waiting, "history": history, "groups": list(groups.values()),
        "labels": appeals_mod.STATUS_LABELS, "preview": preview,
        "keywords": appeals_mod.skip_keywords(s),
        "bc": bc, "camp": camp, "bc_options": bc_options, "bc_names": bc_names, "bc_of": bc_of,
        "n_open_all": len(all_open), "filtered": bool(bc or camp),
    })


@router.post("/appeals/scan")
def scan_now(bc: str = Form(""), camp: str = Form(""), db: Session = Depends(get_db)):
    """Queue the issue scan (which feeds the appeals engine)."""
    from .. import jobs
    job, created = jobs.enqueue_once(db, "issues_scan", "Scan every account for rejected ads", {}, href="/appeals")
    if not created:
        return _back(ok=f"A scan is already {job.status}{' · ' + job.progress if job.progress else ''} — "
                        "it reads every account, give it a few minutes. You can stop it on the Jobs page.", bc=bc, camp=camp)
    return _back(ok="Scanning in the background — you'll get a notification when it's done.", bc=bc, camp=camp)


@router.post("/appeals/refresh")
def refresh_now(bc: str = Form(""), camp: str = Form(""), db: Session = Depends(get_db)):
    from .. import jobs
    waiting = db.query(models.Appeal).filter(models.Appeal.status == "appealing").count()
    if not waiting:
        return _back(ok="No appeals are waiting on TikTok.", bc=bc, camp=camp)
    job, created = jobs.enqueue_once(db, "appeals_refresh", f"Check TikTok's answer on {waiting} open appeal(s)", {}, href="/appeals")
    if not created:
        return _back(ok=f"A check is already {job.status} — hold on.", bc=bc, camp=camp)
    return _back(ok="Checking in the background — you'll get a notification.", bc=bc, camp=camp)


@router.post("/appeals/{row_id}/file")
def file_one(row_id: int, reason: str = Form(""), bc: str = Form(""), camp: str = Form(""),
             db: Session = Depends(get_db)):
    row = db.get(models.Appeal, row_id)
    if not row:
        return _back(err="That rejection is no longer tracked.", bc=bc, camp=camp)
    if row.status == "appealing":
        return _back(err="An appeal is already on file for that ad group.", bc=bc, camp=camp)
    if row.status in ("successful", "done", "failed"):
        return _back(err="TikTok already answered an appeal for that rejection — only one appeal per rejection is allowed.", bc=bc, camp=camp)
    token = _token_for(db, row.advertiser_id)
    if not token:
        return _back(err=f"No TikTok token for account {row.advertiser_name or row.advertiser_id}.", bc=bc, camp=camp)
    from .. import jobs
    jobs.enqueue(db, "appeals_file", f"Appeal “{(row.ad_name or row.adgroup_id)[:50]}”",
                 {"ids": [row.id], "reason": reason.strip()}, href="/appeals")
    return _back(ok="Filing in the background — you'll get a notification with TikTok's answer.", bc=bc, camp=camp)


@router.post("/appeals/file-selected")
async def file_selected(request: Request, db: Session = Depends(get_db)):
    """Appeal only the ticked rejections (one per ad group), optional shared reason."""
    form = await request.form()
    bc, camp = str(form.get("bc") or ""), str(form.get("camp") or "")
    ids = []
    for v in form.getlist("ids"):
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if not ids:
        return _back(err="Tick at least one ad group first.", bc=bc, camp=camp)
    from .. import jobs
    jobs.enqueue(db, "appeals_file", f"Appeal {len(ids)} selected ad group(s)",
                 {"ids": ids, "reason": str(form.get("reason") or "").strip()}, href="/appeals")
    return _back(ok=f"Filing {len(ids)} appeal(s) in the background — you'll get a notification.", bc=bc, camp=camp)


@router.post("/appeals/file-campaign")
def file_campaign(campaign: str = Form(""), reason: str = Form(""), bc: str = Form(""), camp: str = Form(""),
                  db: Session = Depends(get_db)):
    """Appeal every open rejection in one campaign group — every account's copy
    of that campaign name, narrowed by the Business Center / search filter the
    operator is looking at, so the button files exactly the rows it sits above."""
    campaign, bc, camp = campaign.strip(), bc.strip(), camp.strip()
    if not campaign:
        return _back(err="No campaign given.", bc=bc, camp=camp)
    rows = _open_rows_for(db, campaign, bc, camp)
    if not rows:
        return _back(err=f"Nothing open to appeal in “{campaign}” — it was already appealed, dismissed or cleared.",
                     bc=bc, camp=camp)
    from .. import jobs
    n_acc = len({r.advertiser_id for r in rows})
    jobs.enqueue(db, "appeals_file",
                 f"Appeal all {len(rows)} ad group(s) in “{campaign[:50]}” ({n_acc} account{'s' if n_acc != 1 else ''})",
                 {"ids": [r.id for r in rows], "reason": reason.strip()}, href="/appeals")
    return _back(ok=f"Filing {len(rows)} appeal(s) for “{campaign}” in the background — you'll get a notification.",
                 bc=bc, camp=camp)


def file_rows(db: Session, ids: list[int], reason: str = "") -> dict:
    """The filing itself (runs in a job)."""
    s = get_settings(db)
    ok = err = skipped = 0
    for row in db.query(models.Appeal).filter(models.Appeal.id.in_(ids)).all():
        if row.status not in ("pending", "skipped", "error"):
            skipped += 1
            continue
        token = _token_for(db, row.advertiser_id)
        if not token:
            err += 1
            continue
        if appeals_mod.file_appeal(db, row, token, s, filed_by="manual", reason=(reason or None)):
            ok += 1
        else:
            err += 1
    msg = f"Filed {ok} appeal(s)" + (f", {err} refused — see the rows" if err else "") + (f", {skipped} already handled" if skipped else "") + "."
    return {"ok": ok > 0 and err == 0, "detail": msg}


@router.post("/appeals/file-all")
def file_all(db: Session = Depends(get_db)):
    """Appeal every open rejection (pending, skipped and errored ones alike —
    the operator confirmed on the page)."""
    from .. import jobs
    ids = [r.id for r in db.query(models.Appeal.id)
           .filter(models.Appeal.status.in_(OPEN_STATUSES)).all()]
    if not ids:
        return _back(ok="Nothing to appeal.")
    jobs.enqueue(db, "appeals_file", f"Appeal all {len(ids)} open ad group(s)", {"ids": ids, "reason": ""}, href="/appeals")
    return _back(ok=f"Filing {len(ids)} appeal(s) in the background — you'll get a notification.")


@router.post("/appeals/{row_id}/dismiss")
def dismiss(row_id: int, bc: str = Form(""), camp: str = Form(""), db: Session = Depends(get_db)):
    """Operator handled it another way (edited the ad, deleted it, or doesn't care)."""
    row = db.get(models.Appeal, row_id)
    if not row:
        return _back(err="That rejection is no longer tracked.", bc=bc, camp=camp)
    if row.status not in OPEN_STATUSES:
        return _back(err="Only rejections without an appeal on file can be dismissed.", bc=bc, camp=camp)
    row.status = "dismissed"
    row.error = ""
    row.resolved_at = appeals_mod._now()
    db.commit()
    return _back(ok="Dismissed. It comes back only if TikTok reviews the ad group again and rejects it anew.", bc=bc, camp=camp)
