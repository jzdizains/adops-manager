"""/issues — every TikTok-side problem in one place, with deep links to fix."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import issues as issues_mod
from .. import models, queries
from ..database import get_db
from ..templating import render

router = APIRouter()

CATEGORY_META = {
    "payment":  ("💳", "Payment"),
    "account":  ("👤", "Account status"),
    "bc":       ("🏢", "Business Center"),
    "campaign": ("📣", "Campaigns"),
    "ad":       ("🎬", "Ads"),
    "spark":    ("✦", "Spark codes"),
}


@router.get("/issues")
def issues_page(request: Request, db: Session = Depends(get_db)):
    rows = (db.query(models.Issue)
            .order_by(models.Issue.category, models.Issue.advertiser_name).all())
    groups = []
    for key, (icon, label) in CATEGORY_META.items():
        matched = [i for i in rows if i.category == key]
        if matched:
            groups.append({"key": key, "icon": icon, "label": label, "rows": matched})
    scanned_at = queries.get_setting(db, "issues_scanned_at", "")
    return render(request, "issues.html", {
        "title": "Issues", "groups": groups, "total": len(rows),
        "scanned_at": scanned_at[:16].replace("T", " ") if scanned_at else "never",
        "ok": request.query_params.get("ok", ""),
    })


@router.post("/issues/scan")
def scan_now(db: Session = Depends(get_db)):
    result = issues_mod.scan(db)
    return RedirectResponse(
        f"/issues?ok=Scanned+{result['accounts_scanned']}+account(s),+found+{result['issues']}+issue(s)",
        status_code=303)
