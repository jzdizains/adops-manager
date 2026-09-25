"""The Instant Page / Instant Form build queue (v150).

"Build on…" (a page template or a form template, and the accounts that need it) becomes one
AssetBuild row per account. One background job works through them oldest-first, one at a
time, and every step it reaches is written on the row, so the screens show the real step and
how long it has been on it — "stuck?" after STUCK_S.

  page  1) copy a published master page through the page editor's web API and re-point its
           button to the template's link / text (seconds, no browser) — the template's own
           master, else a published page of the same name in the workspace;
        2) only when there is no master, or the copy is refused, the headless-browser builder.
  form  copy the template's master form, write its wording, read EVERY field back, publish
        (lead_form_builder) — a form is never built by browser.

Either way the account is re-read through TikTok's official API and the build only counts
when the page / form is listed there by name. TikTok's hosted preview is kept with it.
A restart marks running builds failed ("interrupted") — Retry puts them back in the queue.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

STUCK_S = 90
KEEP_DAYS = 30


def _now():
    return datetime.utcnow()


def preview_link(asset_id: str, preview_url: str = "") -> str:
    """TikTok's own hosted preview of a page / form (the link /page/get/ gives, else the
    immers.page one Ads Manager opens). Pure."""
    if preview_url:
        return preview_url
    return f"https://sg.immers.page/instant_page/page/{asset_id}?mode=preview&type=wrapped" if asset_id else ""


# ---------------------------------------------------------------------------------------
# who needs it

def has_map(db, models, kind: str, name: str) -> set[str]:
    """Accounts that already hold a page / form of this exact name."""
    M = models.InstantPage if kind == "page" else models.LeadForm
    return {r[0] for r in db.query(M.owner_advertiser_id).filter(M.name == name)}


def group_targets(accounts: list, bc_names: dict, have: set, busy: set) -> list[dict]:
    """[{bc_id, bc_name, accounts: [{id, name, has, busy}], needing}] — BCs with the most
    accounts needing it first, 'No Business Center' last. Pure."""
    groups: dict[str, dict] = {}
    for a in accounts:
        bc = a.owner_bc_id or ""
        g = groups.setdefault(bc, {"bc_id": bc, "bc_name": bc_names.get(bc) or ("No Business Center" if not bc else bc),
                                   "accounts": [], "needing": 0})
        row = {"id": a.advertiser_id, "name": a.advertiser_name or a.advertiser_id,
               "has": a.advertiser_id in have, "busy": a.advertiser_id in busy}
        g["accounts"].append(row)
        g["needing"] += int(not row["has"] and not row["busy"])
    out = sorted(groups.values(), key=lambda g: (g["bc_id"] == "", -g["needing"], g["bc_name"].lower()))
    for g in out:
        g["accounts"].sort(key=lambda r: (r["has"], r["name"].lower()))
    return out


def targets(db, models, sc, kind: str, name: str) -> dict:
    from . import queries
    accts = [a for a in queries.enabled_accounts(db) if sc.allows(a.advertiser_id) and a.access_token]
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter)}
    busy = {r[0] for r in db.query(models.AssetBuild.advertiser_id).filter(
        models.AssetBuild.kind == kind, models.AssetBuild.name == name,
        models.AssetBuild.status.in_(("pending", "running")))}
    groups = group_targets(accts, bc_names, has_map(db, models, kind, name), busy)
    return {"groups": groups, "needing": sum(g["needing"] for g in groups), "total": len(accts)}


# ---------------------------------------------------------------------------------------
# queueing

def queue(db, models, sc, kind: str, template, advertiser_ids: list[str]) -> dict:
    """One row per account that needs it (not already there, not already queued)."""
    have = has_map(db, models, kind, template.name)
    busy = {r[0] for r in db.query(models.AssetBuild.advertiser_id).filter(
        models.AssetBuild.kind == kind, models.AssetBuild.name == template.name,
        models.AssetBuild.status.in_(("pending", "running")))}
    ids = [i for i in dict.fromkeys(str(x) for x in advertiser_ids) if sc.allows(i)]
    todo = [i for i in ids if i not in have and i not in busy]
    batch = f"{kind}-{template.id}-{uuid.uuid4().hex[:8]}"
    for adv in todo:
        db.add(models.AssetBuild(owner_user_id=sc.owner_for_new, kind=kind, template_id=template.id, name=template.name,
                                 advertiser_id=adv, batch=batch, status="pending", step="Waiting in the queue"))
    db.commit()
    if todo:
        kick(db)
    return {"queued": len(todo), "skipped_have": sum(1 for i in ids if i in have), "skipped_busy": sum(1 for i in ids if i in busy),
            "batch": batch}


def kick(db) -> None:
    from . import jobs
    jobs.enqueue_once(db, "asset_builds", "Build Instant Pages / Forms", {}, href="/instant-pages", quiet=True)


def retry(db, models, row) -> bool:
    if row.status not in ("failed", "cancelled"):
        return False
    row.status, row.step, row.error, row.step_at, row.started_at, row.finished_at = "pending", "Waiting in the queue", "", None, None, None
    db.commit()
    kick(db)
    return True


def cancel(db, models, row) -> bool:
    if row.status != "pending":
        return False
    row.status, row.step, row.finished_at = "cancelled", "Cancelled", _now()
    db.commit()
    return True


# ---------------------------------------------------------------------------------------
# running

def _set(db, row, **kw) -> None:
    for k, v in kw.items():
        setattr(row, k, v)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — progress is best-effort; the result write retries below
        db.rollback()


def run(db, models, should_stop=None, on_progress=None) -> dict:
    """The job: every pending build, oldest first, one at a time."""
    from . import spark_web_api
    done = ok = 0
    while True:
        if should_stop and should_stop():
            break
        row = (db.query(models.AssetBuild).filter(models.AssetBuild.status == "pending")
               .order_by(models.AssetBuild.id).first())
        if row is None:
            break
        _set(db, row, status="running", started_at=_now(), step_at=_now(), step="Starting", attempts=(row.attempts or 0) + 1)
        if on_progress:
            on_progress(f"{row.kind} “{row.name}” on {row.advertiser_id}")
        acct = db.query(models.AdAccount).filter_by(advertiser_id=row.advertiser_id).first()

        def step(text, _row=row):
            _set(db, _row, step=str(text)[:200], step_at=_now())
        try:
            if acct is None or not acct.access_token:
                raise RuntimeError("the account isn't connected any more")
            if row.kind == "page":
                res = build_page(db, models, row, acct, step)
            else:
                res = build_form(db, models, row, acct, step)
        except spark_web_api.WebAuthError as e:
            res = {"ok": False, "error": f"The ads.tiktok.com session is dead — refresh the cookies, then Retry. ({str(e)[:120]})", "stop": True}
        except Exception as e:  # noqa: BLE001 — one account's failure is recorded, the queue goes on
            db.rollback()
            res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        if res.get("ok"):
            ok += 1
            _set(db, row, status="success", step="Built", finished_at=_now(), error="", result_id=res.get("id", ""),
                 method=res.get("method", ""), preview_url=preview_link(res.get("id", ""), res.get("preview", "")))
        else:
            _set(db, row, status="failed", step="Failed", finished_at=_now(), error=str(res.get("error") or "failed")[:1000],
                 method=res.get("method", row.method or ""))
        done += 1
        if res.get("stop"):
            # dead cookies: every next build would fail the same way — say so on each waiting row
            # (Retry once the cookies are fresh) instead of leaving them "waiting" with no job
            _close_pending(db, models, "failed", res.get("error") or "the ads.tiktok.com session is dead")
            break
    if should_stop and should_stop():
        _close_pending(db, models, "cancelled", "Stopped from the Jobs page — press Retry to build it.")
    return {"done": done, "ok": ok}


def _close_pending(db, models, status: str, why: str) -> None:
    try:
        for r in db.query(models.AssetBuild).filter(models.AssetBuild.status == "pending"):
            r.status, r.step, r.finished_at, r.error = status, ("Failed" if status == "failed" else "Cancelled"), _now(), str(why)[:1000]
        db.commit()
    except Exception:      # noqa: BLE001
        db.rollback()


def ensure_running(db, models) -> bool:
    """Waiting builds with no job to run them (a restart, a lost wake-up) → queue the job.
    Called at boot and from the slow housekeeping pass."""
    if db.query(models.AssetBuild.id).filter(models.AssetBuild.status == "pending").first() is None:
        return False
    kick(db)
    return True


def active_for(db, models, kind: str, advertiser_id: str, name: str = ""):
    """A pending / running build of this page / form on this account (the launch refuses rather
    than building a second copy of the same name)."""
    q = db.query(models.AssetBuild).filter(models.AssetBuild.kind == kind, models.AssetBuild.advertiser_id == str(advertiser_id),
                                           models.AssetBuild.status.in_(("pending", "running")))
    if name:
        q = q.filter(models.AssetBuild.name == name)
    return q.first()


def _workspace_accounts(db, models, user_id) -> list:
    """The AdAccount rows of a user's workspace: owned + shared through AccountAccess (v155.38).
    `models` is the injected module (tests pass a stub without AccountAccess)."""
    rows = list(db.query(models.AdAccount).filter(models.AdAccount.owner_user_id == user_id))
    access = getattr(models, "AccountAccess", None)
    if access is not None:
        try:
            shared = [r[0] for r in db.query(access.advertiser_id).filter(access.user_id == user_id)]
            if shared:
                have = {a.advertiser_id for a in rows}
                rows += [a for a in db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(shared)) if a.advertiser_id not in have]
        except Exception:  # noqa: BLE001
            db.rollback()
    return rows


def _master_page(db, models, tpl, acct):
    """(page_id, owner) to copy: the template's own master, else a PUBLISHED page of the
    template's name on an account of the same workspace (same Business Center first)."""
    if tpl.master_page_id and tpl.master_advertiser_id:
        return tpl.master_page_id, tpl.master_advertiser_id
    owners = {a.advertiser_id: a for a in _workspace_accounts(db, models, tpl.owner_user_id)} \
        if tpl.owner_user_id is not None else None
    rows = [r for r in db.query(models.InstantPage).filter(models.InstantPage.name == tpl.name,
                                                         models.InstantPage.status == "PUBLISHED",
                                                         models.InstantPage.owner_advertiser_id != acct.advertiser_id)
            if owners is None or r.owner_advertiser_id in owners]
    if not rows:
        return "", ""
    bc_of = {a.advertiser_id: a.owner_bc_id for a in db.query(models.AdAccount).filter(
        models.AdAccount.advertiser_id.in_([r.owner_advertiser_id for r in rows]))}
    rows.sort(key=lambda r: (0 if bc_of.get(r.owner_advertiser_id) == acct.owner_bc_id else 1, r.page_id))
    return rows[0].page_id, rows[0].owner_advertiser_id


def _listed(db, models, M, id_attr, acct, name, want_id=""):
    row = (db.query(M).filter_by(owner_advertiser_id=acct.advertiser_id, **{id_attr: want_id}).first() if want_id else None) \
        or db.query(M).filter_by(owner_advertiser_id=acct.advertiser_id, name=name).first()
    return row


def build_page(db, models, row, acct, step) -> dict:
    from . import instant_page_builder as ipb, instant_page_web, spark_web_api, tiktok_api
    from .routes import instant_pages as ip
    tpl = db.get(models.PageTemplate, row.template_id)
    if tpl is None:
        return {"ok": False, "error": "the page template was deleted"}
    copy_err = ""
    master, owner = _master_page(db, models, tpl, acct)
    if master and spark_web_api.load_cookies():
        step("1/2 Copying the master page and re-pointing its button (read → create → re-point → publish)")
        r = instant_page_web.duplicate(master, tpl.name, acct.advertiser_id, new_url=tpl.url, new_text=tpl.button_text,
                                       source_owner=owner)
        if r.get("ok"):
            step("2/2 Checking TikTok lists it on the account")
            try:
                ip.sync_account(db, acct)
                db.commit()
            except tiktok_api.TikTokError as e:
                db.rollback()
                return {"ok": True, "id": r["page_id"], "method": "web copy",
                        "error": f"copied as {r['page_id']} but the account couldn't be re-read ({e})"}
            got = _listed(db, models, models.InstantPage, "page_id", acct, tpl.name, r["page_id"])
            if got is not None:
                return {"ok": True, "id": got.page_id, "preview": got.preview_url or "", "method": "web copy"}
            copy_err = f"TikTok answered OK for {r['page_id']} but /page/get/ doesn't list it"
        else:
            copy_err = r.get("error") or "TikTok refused the copy"
    elif not master:
        copy_err = "no published page to copy from"
    else:
        copy_err = "no ads.tiktok.com cookies"
    # fallback: the headless-browser builder
    if not ipb.available():
        return {"ok": False, "method": "web copy", "error": f"Copy failed ({copy_err}) and the browser builder isn't installed here."}
    step(f"Copy not possible ({copy_err[:80]}) — building in the browser")
    r = ipb.build_and_verify(db, acct, tpl, on_step=lambda s: step(f"browser · {s}"))
    if r.get("ok"):
        got = _listed(db, models, models.InstantPage, "page_id", acct, tpl.name, r.get("page_id", ""))
        return {"ok": True, "id": r.get("page_id", ""), "preview": (got.preview_url if got else "") or "", "method": "browser"}
    return {"ok": False, "method": "browser", "error": (f"copy: {copy_err}; browser: " if copy_err else "") + str(r.get("error") or "failed"),
            "stop": bool(r.get("challenge"))}


def template_edits(ft) -> dict:
    """A FormTemplate → the builder's edits (only what the template says). Pure."""
    opts = [o.strip() for o in (ft.question_options or "").replace("\r", "").split("\n") if o.strip()]
    return {"destination_url": (ft.destination_url or "").strip(), "cta_title": (ft.cta_title or "").strip(),
            "privacy_url": (ft.privacy_url or "").strip(), "company_name": (ft.company_name or "").strip(),
            "thanks_title": (ft.thanks_title or "").strip(), "thanks_description": (ft.thanks_description or "").strip(),
            "question_label": (ft.question_label or "").strip(), "question_options": opts}


def build_form(db, models, row, acct, step) -> dict:
    from . import lead_form_builder, tiktok_api
    from .routes import lead_forms as lf
    ft = db.get(models.FormTemplate, row.template_id)
    if ft is None:
        return {"ok": False, "error": "the form template was deleted"}
    if not ft.master_form_id:
        return {"ok": False, "error": "the template has no master form to copy — pick one in the template"}
    r = lead_form_builder.build_form(ft.master_form_id, ft.name, acct.advertiser_id, template_edits(ft),
                                     source_owner=ft.master_advertiser_id, on_step=step)
    if not r.get("ok"):
        return {"ok": False, "method": "form copy", "error": r.get("error") or "failed"}
    step("Checking TikTok lists it on the account")
    try:
        lf.sync_account(db, acct)
        db.commit()
    except tiktok_api.TikTokError as e:
        db.rollback()
        return {"ok": True, "id": r["form_id"], "method": "form copy", "error": f"built, but the account couldn't be re-read ({e})"}
    got = _listed(db, models, models.LeadForm, "form_id", acct, ft.name, r["form_id"])
    if got is None:
        return {"ok": False, "method": "form copy", "error": f"TikTok answered OK for {r['form_id']} but doesn't list a form named “{ft.name}” on the account"}
    return {"ok": True, "id": got.form_id, "method": "form copy"}


# ---------------------------------------------------------------------------------------
# reading / housekeeping

def view(rows: list, names: dict, now: datetime | None = None) -> list[dict]:
    """Batches (newest first) with their rows, counts and last failure. Pure."""
    now = now or _now()
    batches: dict[str, dict] = {}
    for r in rows:
        b = batches.setdefault(r.batch, {"batch": r.batch, "kind": r.kind, "name": r.name, "template_id": r.template_id,
                                         "created": r.created_at, "counts": {"pending": 0, "running": 0, "success": 0, "failed": 0, "cancelled": 0},
                                         "rows": [], "last_error": ""})
        b["counts"][r.status] = b["counts"].get(r.status, 0) + 1
        secs = int((now - r.step_at).total_seconds()) if (r.status == "running" and r.step_at) else 0
        b["rows"].append({"id": r.id, "account": names.get(r.advertiser_id) or r.advertiser_id, "advertiser_id": r.advertiser_id,
                          "status": r.status, "step": r.step or "", "secs": secs, "stuck": secs >= STUCK_S,
                          "method": r.method or "", "result_id": r.result_id or "", "preview": r.preview_url or "",
                          "error": r.error or "", "attempts": r.attempts or 0,
                          "at": (r.finished_at or r.started_at or r.created_at).strftime("%b %d %H:%M") if (r.finished_at or r.started_at or r.created_at) else ""})
        if r.status == "failed" and r.error and not b["last_error"]:
            b["last_error"] = r.error
        if r.created_at and (b["created"] is None or r.created_at > b["created"]):
            b["created"] = r.created_at
    out = sorted(batches.values(), key=lambda b: b["created"] or datetime.min, reverse=True)
    for b in out:
        b["created"] = b["created"].strftime("%b %d, %Y %H:%M") if b["created"] else ""
        b["rows"].sort(key=lambda x: ({"running": 0, "pending": 1, "failed": 2, "success": 3, "cancelled": 4}.get(x["status"], 5), x["account"].lower()))
        b["active"] = bool(b["counts"]["pending"] or b["counts"]["running"])
    return out


def recent(db, models, sc, kind: str, limit: int = 400) -> dict:
    q = db.query(models.AssetBuild).filter(models.AssetBuild.kind == kind).order_by(models.AssetBuild.id.desc())
    rows = [r for r in q.limit(limit) if sc.allows(r.advertiser_id)]
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount.advertiser_id, models.AdAccount.advertiser_name)
             .filter(models.AdAccount.advertiser_id.in_(list({r.advertiser_id for r in rows}) or [""]))}
    batches = view(rows, names)
    return {"batches": batches, "active": any(b["active"] for b in batches), "stuck_s": STUCK_S}


def recover(db, models) -> int:
    """At boot: builds a restart cut short."""
    n = 0
    for r in db.query(models.AssetBuild).filter(models.AssetBuild.status == "running"):
        r.status, r.step, r.finished_at = "failed", "Failed", _now()
        r.error = "Interrupted — the server restarted during this build. Press Retry (check the account first: the copy may exist)."
        n += 1
    if n:
        db.commit()
    ensure_running(db, models)          # builds still waiting get their job back
    return n


def prune(db, models, days: int = KEEP_DAYS) -> int:
    n = (db.query(models.AssetBuild).filter(models.AssetBuild.created_at < _now() - timedelta(days=days),
                                            models.AssetBuild.status.in_(("success", "failed", "cancelled")))
         .delete(synchronize_session=False))
    db.commit()
    return int(n or 0)
