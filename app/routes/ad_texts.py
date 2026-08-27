"""Ad-text pool — paste a list of ad texts (one per line); each is consumed by
exactly ONE launch so every campaign carries unique copy."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..templating import render

router = APIRouter()

MAX_TEXT_LEN = 100   # TikTok ad text limit


@router.get("/ad-texts")
def ad_texts_page(request: Request, db: Session = Depends(get_db)):
    rows = (db.query(models.AdText)
            .order_by(models.AdText.status, models.AdText.id.desc()).all())
    accounts = {a.advertiser_id: (a.advertiser_name or a.advertiser_id)
                for a in db.query(models.AdAccount).all()}
    available = sum(1 for r in rows if r.status == "available")
    return render(request, "ad_texts.html", {
        "rows": rows, "accounts": accounts, "available": available,
        "max_len": MAX_TEXT_LEN,
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
        "title": "Ad Texts",
    })


@router.post("/ad-texts/add")
async def add_texts(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    lines = [ln.strip() for ln in str(form.get("texts") or "").splitlines() if ln.strip()]
    existing = {t.text for t in db.query(models.AdText).all()}
    saved, skipped = 0, 0
    for ln in lines:
        if len(ln) > MAX_TEXT_LEN:
            ln = ln[:MAX_TEXT_LEN]
        if ln in existing:
            skipped += 1
            continue
        db.add(models.AdText(text=ln))
        existing.add(ln)
        saved += 1
    db.commit()
    q = f"ok={saved}+added"
    if skipped:
        q += f"&err={skipped}+duplicate(s)+skipped"
    return RedirectResponse(f"/ad-texts?{q}", status_code=303)


@router.post("/ad-texts/{text_id}/update")
async def update_text(text_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    row = db.get(models.AdText, text_id)
    if not row:
        return RedirectResponse("/ad-texts?err=not+found", status_code=303)
    if row.status == "used":
        return RedirectResponse("/ad-texts?err=already+used+—+locked", status_code=303)
    text = str(form.get("text") or "").strip()[:MAX_TEXT_LEN]
    if text:
        row.text = text
        db.commit()
    return RedirectResponse("/ad-texts?ok=saved", status_code=303)


@router.post("/ad-texts/{text_id}/delete")
def delete_text(text_id: int, db: Session = Depends(get_db)):
    row = db.get(models.AdText, text_id)
    if not row:
        return RedirectResponse("/ad-texts?err=not+found", status_code=303)
    if row.status == "used":
        return RedirectResponse("/ad-texts?err=already+used+—+kept+for+history",
                                status_code=303)
    db.delete(row)
    db.commit()
    return RedirectResponse("/ad-texts?ok=deleted", status_code=303)
