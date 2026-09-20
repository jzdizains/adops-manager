"""TikTok Instant Pages (custom pages) — synced per ad account.

A page belongs to ONE ad account (/page/get/ takes an advertiser_id; the
Marketing API has no share/copy call). The app copes by NAME: a preset stores
the page's name and each launch resolves that account's own page with the
same name. The clone button rides the cookie web path (ads.tiktok.com), which
TikTok doesn't document — it needs the TikTok Cookies page set up and can
stop working without notice.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import models, queries, spark_web_api, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


def _page_names_by_account(db: Session) -> dict[str, set]:
    out: dict[str, set] = {}
    for name, adv in db.query(models.InstantPage.name, models.InstantPage.owner_advertiser_id):
        out.setdefault(name, set()).add(adv)
    return out

STATUS_LABELS = {"PUBLISHED": "Published", "EDITED": "Draft"}


def sync_account(db: Session, acct: models.AdAccount) -> int:
    """Refresh one account's rows from TikTok; drops rows TikTok no longer
    lists. Returns how many pages the account has. Raises TikTokError."""
    items = tiktok_api.list_all_instant_pages(acct.access_token, acct.advertiser_id)
    seen = set()
    for p in items:
        pid = str(p.get("page_id") or "")
        if not pid:
            continue
        seen.add(pid)
        row = (db.query(models.InstantPage)
               .filter_by(page_id=pid, owner_advertiser_id=acct.advertiser_id).first())
        if not row:
            row = models.InstantPage(page_id=pid, owner_advertiser_id=acct.advertiser_id)
            db.add(row)
        row.name = p.get("title") or row.name or ""
        row.status = str(p.get("status") or "")
        row.preview_url = p.get("preview_url") or ""
    gone = (db.query(models.InstantPage)
            .filter(models.InstantPage.owner_advertiser_id == acct.advertiser_id).all())
    for row in gone:
        if row.page_id not in seen:
            db.delete(row)
    return len(seen)


def _mark_owner(sc) -> int | None:
    """Whose favourites/tags a view uses: the workspace in view, else the logged-in user
    (the super admin on "Everyone" keeps their own marks)."""
    return sc.user_id if sc.user_id is not None else sc.me_id


def _marks_for(db: Session, owner: int | None) -> dict[str, dict]:
    from .. import instant_pages_view as ipv
    if owner is None:
        return {}
    return {m.page_name: {"favorite": bool(m.favorite), "tag_ids": ipv.parse_tag_ids(m.tag_ids)}
            for m in db.query(models.InstantPageMark).filter(models.InstantPageMark.owner_user_id == owner)}


@router.get("/instant-pages")
def page(request: Request, db: Session = Depends(get_db)):
    from .. import scope as scope_mod, instant_pages_view as ipv, tags as tags_mod
    sc = scope_mod.for_request(request, db)
    pages = [p for p in db.query(models.InstantPage).order_by(models.InstantPage.name, models.InstantPage.owner_advertiser_id).all() if sc.allows(p.owner_advertiser_id)]
    accounts = [a for a in queries.enabled_accounts(db) if sc.allows(a.advertiser_id)]
    bc_names = {b.bc_id: (b.name or b.bc_id) for b in db.query(models.BusinessCenter).all()}
    owner = _mark_owner(sc)
    tags = tags_mod.all_tags(db, sc.user_id)
    grouped = ipv.group_pages(pages, accounts, bc_names, _marks_for(db, owner), {t["id"]: t for t in tags})
    have: dict[str, set] = {g["name"]: {c["adv"] for c in g["copies"]} for g in grouped["groups"]}
    by_bc: dict[str, list] = {}
    for a in accounts:
        if a.owner_bc_id:
            by_bc.setdefault(a.owner_bc_id, []).append(a.advertiser_id)
    bcs = [b for b in grouped["bcs"] if b["bc_id"]]          # the template builder's BC choices
    # page templates (this workspace) with coverage: how many enabled accounts already hold a page of that name
    from .. import instant_page_builder as ipb
    templates = sc.owned(db.query(models.PageTemplate), models.PageTemplate).order_by(models.PageTemplate.name).all()
    tpl_cov = {t.id: sum(1 for a in accounts if a.advertiser_id in have.get(t.name, set())) for t in templates}
    tpl_missing = {t.id: {bc: sum(1 for aid in ids if aid not in have.get(t.name, set())) for bc, ids in by_bc.items()} for t in templates}
    return render(request, "instant_pages.html", {
        "groups": grouped["groups"], "page_data": ipv.page_json(grouped), "tags": tags, "accounts": accounts,
        "title": "Instant Pages", "status_labels": STATUS_LABELS, "bcs": bcs,
        "templates": templates, "tpl_cov": tpl_cov, "tpl_missing": tpl_missing, "shots": recent_shots(db, sc),
        "builder_ready": ipb.available() and bool(spark_web_api.load_cookies()), "builder_installed": ipb.available(),
        "web_ready": bool(spark_web_api.load_cookies()),
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/instant-pages/mark")
async def mark(request: Request, db: Session = Depends(get_db)):
    """Favourite / tag a page NAME for this workspace (fetch; answers JSON so the row
    updates in place). Fields: name; favorite=1|0 to set the star; tag_id + on=1|0 to
    put a tag on or take it off."""
    from fastapi.responses import JSONResponse
    from .. import scope as scope_mod, instant_pages_view as ipv, tags as tags_mod
    from ..database import safe_commit
    form = await request.form()
    name = str(form.get("name") or "").strip()[:200]
    if not name:
        return JSONResponse({"ok": False, "error": "Which page?"}, status_code=400)
    sc = scope_mod.for_request(request, db)
    owner = _mark_owner(sc)
    if owner is None:
        return JSONResponse({"ok": False, "error": "No workspace to save this in."}, status_code=400)
    row = (db.query(models.InstantPageMark)
           .filter(models.InstantPageMark.owner_user_id == owner, models.InstantPageMark.page_name == name).first())
    if row is None:
        row = models.InstantPageMark(owner_user_id=owner, page_name=name, favorite=False, tag_ids="[]")
        db.add(row)
    if form.get("favorite") is not None:
        row.favorite = str(form.get("favorite")) in ("1", "true", "on", "yes")
    tag_ids = ipv.parse_tag_ids(row.tag_ids)
    if form.get("tag_id") is not None:
        try:
            tid = int(form.get("tag_id") or 0)
        except (TypeError, ValueError):
            tid = 0
        if not tid or tags_mod.get_in_view(db, tid, sc.user_id) is None:
            return JSONResponse({"ok": False, "error": "That tag isn't in this workspace."}, status_code=404)
        on = str(form.get("on") or "1") in ("1", "true", "on", "yes")
        if on and tid not in tag_ids:
            tag_ids.append(tid)
        if not on:
            tag_ids = [t for t in tag_ids if t != tid]
        row.tag_ids = json.dumps(tag_ids)
    if not safe_commit(db):
        return JSONResponse({"ok": False, "error": "The dashboard is busy for a moment — try again."}, status_code=503)
    by_id = {t["id"]: t for t in tags_mod.all_tags(db, sc.user_id)}
    return JSONResponse({"ok": True, "name": name, "favorite": bool(row.favorite),
                         "tags": [by_id[t] for t in tag_ids if t in by_id]})


@router.post("/instant-pages/sync")
def sync(db: Session = Depends(get_db)):
    accounts = queries.enabled_accounts(db)
    if not accounts:
        return RedirectResponse("/instant-pages?err=" + quote("No enabled ad accounts — connect TikTok first."), status_code=303)
    total, ok, failed = 0, 0, []
    for acct in accounts:
        try:
            total += sync_account(db, acct)
            db.commit()                      # per account, so one failure can't undo the others
            ok += 1
        except tiktok_api.TikTokError as e:
            db.rollback()
            failed.append(f"{acct.advertiser_name or acct.advertiser_id}: {e}")
    msg = f"Found {total} instant page(s) across {ok} account(s)."
    if failed:
        shown = "; ".join(failed[:3]) + (f"; +{len(failed) - 3} more" if len(failed) > 3 else "")
        msg += f" {len(failed)} account(s) couldn't be read — {shown}"
        if not ok:
            return RedirectResponse("/instant-pages?err=" + quote(msg), status_code=303)
    return RedirectResponse("/instant-pages?ok=" + quote(msg), status_code=303)


# ---------------------------------------------------------------------------
# Page templates — built on accounts by driving TikTok's own builder (instant_page_builder)
# ---------------------------------------------------------------------------

def _tpl(db: Session, sc, tpl_id: int):
    t = db.get(models.PageTemplate, int(tpl_id))
    return t if (t is not None and sc.owns(t)) else None


def _builder_gate() -> str:
    from .. import instant_page_builder as ipb
    if not ipb.available():
        return "The page builder needs Playwright + Chromium in this deployment (requirements.txt has it; the build runs `playwright install chromium`)."
    if not spark_web_api.load_cookies():
        return "The page builder drives Ads Manager with your web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."
    return ""


@router.post("/instant-pages/templates/save")
def template_save(request: Request, db: Session = Depends(get_db), tpl_id: str = Form(""), name: str = Form(...),
                  button_text: str = Form("Continue"), url: str = Form(...), button_color: str = Form(""),
                  hand_cursor: str = Form(""), bottom_fixed: str = Form(""), color_scheme: str = Form("light")):
    from .. import scope as scope_mod
    sc = scope_mod.for_request(request, db)
    name, url = name.strip()[:100], url.strip()
    if not name:
        return RedirectResponse("/instant-pages?err=" + quote("The template needs a name — it becomes the page's name on every account."), status_code=303)
    if not url.startswith(("http://", "https://")):
        return RedirectResponse("/instant-pages?err=" + quote("The destination URL must start with http:// or https://"), status_code=303)
    t = _tpl(db, sc, int(tpl_id)) if str(tpl_id).isdigit() else None
    if t is None:
        t = models.PageTemplate(owner_user_id=sc.owner_for_new)
        db.add(t)
    t.name, t.button_text, t.url = name, (button_text.strip() or "Continue")[:40], url[:2000]
    col = button_color.strip()
    t.button_color = col if (col.startswith("#") and len(col) == 7) else ""
    t.hand_cursor, t.bottom_fixed = bool(hand_cursor), bool(bottom_fixed)
    t.color_scheme = "dark" if color_scheme == "dark" else "light"
    db.commit()
    return RedirectResponse("/instant-pages?ok=" + quote(f"Template “{t.name}” saved.") + "#templates", status_code=303)


@router.post("/instant-pages/templates/{tpl_id}/delete")
def template_delete(request: Request, tpl_id: int, db: Session = Depends(get_db)):
    from .. import scope as scope_mod
    t = _tpl(db, scope_mod.for_request(request, db), tpl_id)
    if t is None:
        return RedirectResponse("/instant-pages?err=" + quote("That template is not in your workspace."), status_code=303)
    db.delete(t)
    db.commit()
    return RedirectResponse("/instant-pages?ok=" + quote("Template removed. Pages already built from it stay on their accounts.") + "#templates", status_code=303)


@router.post("/instant-pages/templates/{tpl_id}/build")
def template_build(request: Request, tpl_id: int, advertiser_id: str = Form(...), db: Session = Depends(get_db)):
    """Build the template's page on ONE account (the test button) — as a job."""
    from .. import jobs, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    t = _tpl(db, sc, tpl_id)
    if t is None or not sc.allows(advertiser_id):
        return RedirectResponse("/instant-pages?err=" + quote("That template or account is not in your workspace."), status_code=303)
    gate = _builder_gate()
    if gate:
        return RedirectResponse("/instant-pages?err=" + quote(gate), status_code=303)
    acct = db.query(models.AdAccount).filter_by(advertiser_id=advertiser_id).first()
    label = (acct.advertiser_name if acct else "") or advertiser_id
    job = jobs.enqueue(db, "instant_page_build", f"Build page “{t.name}” on {label}",
                       {"template_id": t.id, "targets": [advertiser_id]}, href="/instant-pages")
    return RedirectResponse("/instant-pages?ok=" + quote(f"Building “{t.name}” on {label} in the background (job #{job.id}) — you'll get a notification with the result and screenshots.") + "#templates", status_code=303)


@router.post("/instant-pages/templates/{tpl_id}/build-bc")
def template_build_bc(request: Request, tpl_id: int, bc_id: str = Form(...), db: Session = Depends(get_db)):
    """Build the page on every enabled account of a Business Center (in view) that lacks a page of that name."""
    from .. import jobs, scope as scope_mod
    sc = scope_mod.for_request(request, db)
    t = _tpl(db, sc, tpl_id)
    if t is None:
        return RedirectResponse("/instant-pages?err=" + quote("That template is not in your workspace."), status_code=303)
    gate = _builder_gate()
    if gate:
        return RedirectResponse("/instant-pages?err=" + quote(gate), status_code=303)
    have = _page_names_by_account(db).get(t.name, set())
    targets = [a.advertiser_id for a in queries.enabled_accounts(db)
               if a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id not in have]
    bc = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
    bc_name = (bc.name if bc else "") or bc_id
    if not targets:
        return RedirectResponse("/instant-pages?ok=" + quote(f"Every enabled account in {bc_name} already has “{t.name}”.") + "#templates", status_code=303)
    job = jobs.enqueue(db, "instant_page_build", f"Build page “{t.name}” on {len(targets)} account(s) in {bc_name}",
                       {"template_id": t.id, "targets": targets}, href="/instant-pages")
    return RedirectResponse("/instant-pages?ok=" + quote(
        f"Building “{t.name}” on {len(targets)} account(s) in {bc_name} — one browser at a time, about a minute each (job #{job.id}). "
        "Stop it any time from Jobs.") + "#templates", status_code=303)


def build_on_accounts(db: Session, template_id: int, targets: list[str], should_stop=None, on_progress=None) -> dict:
    """The job body: build + verify on each account in turn. A build TikTok refuses is
    reported per account and never repeated blindly; a verification challenge stops the
    whole run — a human has to look."""
    from .. import instant_page_builder as ipb
    t = db.get(models.PageTemplate, int(template_id))
    if t is None:
        return {"ok": [], "failed": ["template no longer exists"], "stopped": False, "shots": [], "name": ""}
    ok, failed, shots, stopped = [], [], [], False
    accts = {a.advertiser_id: a for a in db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(targets or [""])).all()}
    for i, adv in enumerate(targets):
        if should_stop and should_stop():
            stopped = True
            break
        acct = accts.get(adv)
        label = (acct.advertiser_name if acct else "") or adv
        if acct is None or not acct.access_token:
            failed.append(f"{label}: account not connected")
            continue
        r = ipb.build_and_verify(db, acct, t, on_step=lambda s, l=label, i=i: on_progress and on_progress(f"{i + 1} of {len(targets)} — {l}: {s}"))
        shots.extend(s.get("shot", "") for s in r.get("steps", []) if s.get("shot"))
        if r.get("ok"):
            ok.append(adv)
        else:
            failed.append(f"{label}: {r.get('error', 'failed')}")
            if r.get("challenge"):
                failed.append("stopped — TikTok asked for verification; open Ads Manager yourself once, then retry")
                stopped = True
                break
            if r.get("retry"):
                stopped = True          # memory guard: try again later rather than churn
                break
    return {"ok": ok, "failed": failed, "stopped": stopped, "shots": shots, "name": t.name}


def recent_shots(db: Session, sc, limit: int = 8) -> list[dict]:
    """The newest build screenshots for accounts in this view: [{name, adv, label, tag, at}]."""
    from .. import instant_page_builder as ipb
    import re as _re
    out = []
    try:
        files = sorted(ipb.SHOT_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return out
    names = {a.advertiser_id: a.advertiser_name for a in db.query(models.AdAccount).all()}
    for p in files:
        m = _re.fullmatch(r"([0-9]{6,25})_([0-9T]+)_([a-z-]+)\.png", p.name)
        if not m or not sc.allows(m.group(1)):
            continue
        out.append({"name": p.name, "adv": m.group(1), "label": names.get(m.group(1)) or m.group(1), "tag": m.group(3),
                    "at": m.group(2).replace("T", " ")[:15]})
        if len(out) >= limit:
            break
    return out


@router.get("/instant-pages/builds/{shot}")
def build_shot(request: Request, shot: str, db: Session = Depends(get_db)):
    """One screenshot from a build. Behind the login like every page, and only for an
    account in the viewer's own workspace (the file name starts with the advertiser id)."""
    import re as _re
    from .. import instant_page_builder as ipb, scope as scope_mod
    m = _re.fullmatch(r"([0-9]{6,25})_[0-9T]+_[a-z-]+\.png", shot)
    if not m:
        return Response(status_code=404)
    if not scope_mod.for_request(request, db).allows(m.group(1)):
        return Response(status_code=404)
    p = ipb.SHOT_DIR / shot
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/instant-pages/clone-bc")
def clone_bc(request: Request, page_id: str = Form(...), from_advertiser_id: str = Form(...), bc_id: str = Form(...),
             new_url: str = Form(""), new_text: str = Form(""), db: Session = Depends(get_db)):
    """One button: clone the page to EVERY enabled account of a Business Center (in this
    view) that doesn't already hold a page with the same name. Runs as a job — one web
    call per account, paced — and reports per-account results."""
    from .. import jobs, scope as scope_mod
    if not spark_web_api.load_cookies():
        return RedirectResponse("/instant-pages?err=" + quote(
            "Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    sc = scope_mod.for_request(request, db)
    src = db.query(models.InstantPage).filter_by(page_id=page_id, owner_advertiser_id=from_advertiser_id).first()
    if src is None or not sc.allows(from_advertiser_id):
        return RedirectResponse("/instant-pages?err=" + quote("That page is no longer listed — sync and try again."), status_code=303)
    have = {p.owner_advertiser_id for p in db.query(models.InstantPage).filter_by(name=src.name).all()}
    targets = [a for a in queries.enabled_accounts(db)
               if a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id != from_advertiser_id and a.advertiser_id not in have]
    bc = db.query(models.BusinessCenter).filter_by(bc_id=bc_id).first()
    bc_name = (bc.name if bc else "") or bc_id
    if not targets:
        return RedirectResponse("/instant-pages?ok=" + quote(f"Every enabled account in {bc_name} already has “{src.name}” — nothing to clone."), status_code=303)
    new_url = (new_url or "").strip()
    if new_url and not new_url.lower().startswith(("http://", "https://")):
        return RedirectResponse("/instant-pages?err=" + quote("The new button link must start with http:// or https://."), status_code=303)
    job = jobs.enqueue(db, "instant_page_clone_all", f"Clone “{src.name}” → {len(targets)} account(s) in {bc_name}",
                       {"page_id": page_id, "from_advertiser_id": from_advertiser_id, "name": src.name,
                        "targets": [a.advertiser_id for a in targets], "new_url": new_url, "new_text": (new_text or "").strip()},
                       href="/instant-pages")
    return RedirectResponse("/instant-pages?ok=" + quote(
        f"Cloning “{src.name}” to {len(targets)} account(s) in {bc_name} in the background — you'll get a notification (job #{job.id})."), status_code=303)


def clone_one(db: Session, page_id: str, name: str, acct: models.AdAccount, new_url: str = "", new_text: str = "",
              source_owner: str = "") -> dict:
    """Copy one page onto one account through the page editor's web API (instant_page_web,
    the recorded duplicate → optional re-point → publish flow), then VERIFY through the
    official /page/get/ that a page of that name now exists there. Returns {ok, page_id,
    error}. Raises WebAuthError when the cookies are dead — the caller stops the run."""
    from .. import instant_page_web
    if not source_owner:
        src = db.query(models.InstantPage).filter_by(page_id=page_id).first()
        source_owner = src.owner_advertiser_id if src else ""
    r = instant_page_web.duplicate(page_id, name, acct.advertiser_id, new_url=new_url, new_text=new_text, source_owner=source_owner)
    if not r.get("ok"):
        return {"ok": False, "page_id": "", "error": r.get("error", "TikTok refused the copy")}
    try:
        sync_account(db, acct)
        db.commit()
    except tiktok_api.TikTokError as e:
        db.rollback()
        return {"ok": True, "page_id": r["page_id"], "error": f"copied as {r['page_id']} but the account couldn't be re-read ({e}) — Sync later"}
    row = (db.query(models.InstantPage)
           .filter_by(owner_advertiser_id=acct.advertiser_id, page_id=r["page_id"]).first()
           or db.query(models.InstantPage).filter_by(owner_advertiser_id=acct.advertiser_id, name=name).first())
    if row is None:
        return {"ok": False, "page_id": r["page_id"], "error": f"TikTok answered OK for page {r['page_id']} but /page/get/ doesn't list it on the account"}
    return {"ok": True, "page_id": row.page_id, "error": ""}


def clone_to_many(db: Session, page_id: str, from_advertiser_id: str, targets: list[str], should_stop=None, on_progress=None,
                  name: str = "", new_url: str = "", new_text: str = "") -> dict:
    """The job body: copy to each target and verify it, one account at a time with a short
    pause; a refused account never stops the rest, dead cookies stop the whole run."""
    import time as _time
    ok, failed, notes, stopped = [], [], [], False
    accts = {a.advertiser_id: a for a in db.query(models.AdAccount).filter(models.AdAccount.advertiser_id.in_(targets or [""])).all()}
    if not name:
        src = db.query(models.InstantPage).filter_by(page_id=page_id, owner_advertiser_id=from_advertiser_id).first()
        name = (src.name if src else "") or f"page {page_id}"
    for i, adv in enumerate(targets):
        if should_stop and should_stop():
            stopped = True
            break
        acct = accts.get(adv)
        label = (acct.advertiser_name if acct else "") or adv
        if on_progress:
            on_progress(f"{i + 1} of {len(targets)} — {label}")
        if acct is None:
            failed.append(f"{label}: not an account of this dashboard")
            continue
        # a re-run never duplicates: an account that already lists the name is skipped
        if db.query(models.InstantPage).filter_by(owner_advertiser_id=adv, name=name).first() is not None:
            ok.append(adv)
            continue
        try:
            r = clone_one(db, page_id, name, acct, new_url=new_url, new_text=new_text, source_owner=from_advertiser_id)
        except spark_web_api.WebAuthError as e:
            failed.append(f"{label}: {str(e)[:120]} — stopped here, the remaining accounts were not attempted")
            stopped = True
            break
        if r["ok"]:
            ok.append(adv)
            if r.get("error"):
                notes.append(f"{label}: {r['error'][:140]}")
        else:
            failed.append(f"{label}: {r['error'][:140]}")
        if i + 1 < len(targets):
            _time.sleep(1.5)
    return {"ok": ok, "failed": failed, "stopped": stopped, "notes": notes}


@router.post("/instant-pages/clone")
def clone(request: Request, page_id: str = Form(...), from_advertiser_id: str = Form(...),
          to_advertiser_id: str = Form(...), new_url: str = Form(""), new_text: str = Form(""), db: Session = Depends(get_db)):
    """Copy one page onto ONE account (the test before "Clone to all"), optionally
    re-pointing its button. Verified through /page/get/ before it is called done."""
    from .. import scope as scope_mod
    if not spark_web_api.load_cookies():
        return RedirectResponse("/instant-pages?err=" + quote(
            "Cloning uses the TikTok web session — paste your ads.tiktok.com cookies on the TikTok Cookies page first."), status_code=303)
    sc = scope_mod.for_request(request, db)
    src = db.query(models.InstantPage).filter_by(page_id=page_id, owner_advertiser_id=from_advertiser_id).first()
    target = db.query(models.AdAccount).filter_by(advertiser_id=to_advertiser_id).first()
    if src is None or target is None or not sc.allows(from_advertiser_id) or not sc.allows(to_advertiser_id):
        return RedirectResponse("/instant-pages?err=" + quote("That page or account is no longer listed — sync and try again."), status_code=303)
    new_url = (new_url or "").strip()
    if new_url and not new_url.lower().startswith(("http://", "https://")):
        return RedirectResponse("/instant-pages?err=" + quote("The new button link must start with http:// or https://."), status_code=303)
    label = target.advertiser_name or to_advertiser_id
    try:
        r = clone_one(db, page_id, src.name, target, new_url=new_url, new_text=(new_text or "").strip(), source_owner=from_advertiser_id)
    except spark_web_api.WebAuthError as e:
        return RedirectResponse("/instant-pages?err=" + quote(f"Clone stopped: {str(e)[:200]}"), status_code=303)
    if not r["ok"]:
        return RedirectResponse("/instant-pages?err=" + quote(f"Clone to {label} failed — {r['error'][:220]}. The full answer is on Diagnostics."), status_code=303)
    msg = f"“{src.name}” is now on {label} as page {r['page_id']}, published" + (f" — {r['error']}" if r.get("error") else ".")
    if new_url:
        msg += " Open it in Ads Manager and check the button link before cloning everywhere."
    return RedirectResponse("/instant-pages?ok=" + quote(msg), status_code=303)
