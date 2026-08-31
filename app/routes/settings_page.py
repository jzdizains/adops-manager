"""/settings — every runtime-tunable knob in one place. Changes apply within
one sweep (no redeploy needed)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import config
from ..database import get_db
from ..settings_store import get_settings, save_settings
from ..templating import render

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    s = get_settings(db)
    base_url = str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
        base_url = "https://" + base_url[len("http://"):]
    postback_template = (
        f"{base_url}/postback?key={s['postback_key']}"
        "&source={source}&revenue={payout}&txn={transaction_id}"
        "&event=purchase&ttclid={ttclid}"
    )
    return render(request, "settings.html", {
        "title": "Settings", "s": s,
        "postback_template": postback_template,
        "ok": request.query_params.get("ok", ""),
        "tz": config.BUSINESS_TZ,
    })


@router.post("/settings/save")
async def save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    current = get_settings(db)
    values = dict(current)
    for key in current:
        if key == "postback_key":
            continue  # never editable from the form
        if isinstance(current[key], bool):
            values[key] = form.get(key) is not None          # checkbox present = on
        elif key in form:
            values[key] = form.get(key)
    save_settings(db, values)
    return RedirectResponse("/settings?ok=Saved.+Changes+apply+within+one+sweep.", status_code=303)
