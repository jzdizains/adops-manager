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
    """Merged into Health — keep the old URL working."""
    return RedirectResponse("/monitor?view=issues", status_code=303)


@router.post("/issues/scan")
def scan_now(db: Session = Depends(get_db)):
    from .. import jobs
    jobs.enqueue(db, "issues_scan", "Scan every account for issues", {}, href="/monitor?view=issues")
    return RedirectResponse("/monitor?view=issues&ok=Scanning+in+the+background+—+you%27ll+get+a+notification.", status_code=303)
