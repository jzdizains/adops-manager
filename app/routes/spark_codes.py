"""Spark codes list CRUD + the Spark Hub (auto-grab from connected creators).

Auto-grab (§5): list_identities → for each creator identity, list_tt_videos
(returns only AD-AUTHORIZED posts — §9.4) → store each item's auth_code,
item_id, media type, thumbnail and post link as SparkCode rows grouped by
creator. Hand-entered codes leave tiktok_item_id empty.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models, queries, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/spark-codes")
def spark_list(request: Request, db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip().lower()
    mine_only = request.query_params.get("mine", "") == "1"
    groups = db.query(models.SparkCodeGroup).order_by(models.SparkCodeGroup.name).all()
    my_creators = {s.creator_handle for s in
                   db.query(models.SparkSetting).filter_by(is_mine=True).all()}
    out_groups = []
    for g in groups:
        if mine_only and my_creators and g.name not in my_creators:
            continue
        codes = [c for c in g.codes
                 if not q or q in (c.name or "").lower() or q in (c.code or "").lower()]
        if codes or not q:
            out_groups.append({"g": g, "codes": codes})
    ungrouped = (db.query(models.SparkCode).filter(models.SparkCode.group_id.is_(None))
                 .order_by(models.SparkCode.created_at.desc()).all())
    if q:
        ungrouped = [c for c in ungrouped
                     if q in (c.name or "").lower() or q in (c.code or "").lower()]
    return render(request, "spark_codes.html", {
        "groups": out_groups, "ungrouped": ungrouped, "q": q, "mine_only": mine_only,
        "my_creators": my_creators, "title": "Spark Codes",
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.post("/spark-codes/add")
def add_code(name: str = Form(""), code: str = Form(...), media_type: str = Form("VIDEO"),
             tiktok_post_url: str = Form(""), group_name: str = Form(""),
             db: Session = Depends(get_db)):
    group = None
    if group_name.strip():
        group = db.query(models.SparkCodeGroup).filter_by(name=group_name.strip()).first()
        if not group:
            group = models.SparkCodeGroup(name=group_name.strip())
            db.add(group)
            db.flush()
    db.add(models.SparkCode(name=name.strip(), code=code.strip(), media_type=media_type,
                            tiktok_post_url=tiktok_post_url.strip(),
                            group_id=group.id if group else None))
    db.commit()
    return RedirectResponse("/spark-codes?ok=added", status_code=303)


@router.post("/spark-codes/{code_id}/delete")
def delete_code(code_id: int, db: Session = Depends(get_db)):
    c = db.get(models.SparkCode, code_id)
    if c:
        db.delete(c)
        db.commit()
    return RedirectResponse("/spark-codes?ok=deleted", status_code=303)


@router.post("/spark-codes/{code_id}/status")
def set_status(code_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    c = db.get(models.SparkCode, code_id)
    if c and status in ("active", "used", "expired"):
        c.status = status
        db.commit()
    return RedirectResponse("/spark-codes", status_code=303)


# ---------------------------------------------------------------------------
# Spark Hub — auto-grab
# ---------------------------------------------------------------------------

@router.post("/spark-codes/grab")
def auto_grab(request: Request, db: Session = Depends(get_db)):
    """Pull every connected creator's ad-authorized posts into SparkCode rows."""
    token = queries.any_access_token(db)
    if not token:
        return RedirectResponse("/spark-codes?err=Connect+TikTok+first", status_code=303)
    accounts = queries.enabled_accounts(db)
    grabbed, seen_items = 0, {c.tiktok_item_id for c in db.query(models.SparkCode).all() if c.tiktok_item_id}
    errors = []
    for acct in accounts:
        try:
            identities = tiktok_api.list_identities(acct.access_token, acct.advertiser_id)
        except tiktok_api.TikTokError as e:
            errors.append(f"{acct.advertiser_id}: {e.code}")
            continue
        for ident in identities:
            itype = ident.get("identity_type", "")
            if itype not in ("TT_USER", "BC_AUTH_TT"):
                continue  # only real creator identities carry grabbable posts
            handle = ident.get("display_name", "") or ident.get("identity_id", "")
            try:
                data = tiktok_api.list_tt_videos(acct.access_token, acct.advertiser_id,
                                                 ident["identity_id"], itype)
            except tiktok_api.TikTokError:
                continue
            for item in data.get("list", []):
                info = item.get("item_info", item)
                item_id = str(info.get("item_id", ""))
                if not item_id or item_id in seen_items:
                    continue
                seen_items.add(item_id)
                group = db.query(models.SparkCodeGroup).filter_by(name=handle).first()
                if not group:
                    group = models.SparkCodeGroup(name=handle)
                    db.add(group)
                    db.flush()
                db.add(models.SparkCode(
                    name=(info.get("text", "") or "")[:80] or f"{handle} · {item_id[-6:]}",
                    code=info.get("auth_code", ""),
                    media_type=("CAROUSEL" if str(info.get("item_type", "")).upper() == "CAROUSEL"
                                else "VIDEO"),
                    tiktok_post_url=info.get("share_url", "") or
                                    f"https://www.tiktok.com/@{handle}/video/{item_id}",
                    thumbnail_url=(info.get("video_cover_url") or info.get("poster_url") or ""),
                    tiktok_item_id=item_id,
                    group_id=group.id,
                ))
                grabbed += 1
    db.commit()
    msg = f"grabbed+{grabbed}" + (f"&err={len(errors)}+accounts+failed" if errors else "")
    return RedirectResponse(f"/spark-codes?ok={msg}", status_code=303)


@router.post("/spark-codes/creators/toggle")
def toggle_creator(creator_handle: str = Form(...), db: Session = Depends(get_db)):
    row = db.query(models.SparkSetting).filter_by(creator_handle=creator_handle).first()
    if row:
        row.is_mine = not row.is_mine
    else:
        db.add(models.SparkSetting(creator_handle=creator_handle, is_mine=True))
    db.commit()
    return RedirectResponse("/spark-codes", status_code=303)
