"""/assistant — chat with Claude over the dashboard's data (see app/assistant.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import assistant as asst, config, models
from ..database import get_db
from ..settings_store import get_settings
from ..templating import render

router = APIRouter()

SUGGESTIONS = [
    "7-day P&L per Business Center — which one is losing money?",
    "Which campaigns are under 0.8× ROAS today with more than $50 spent? Propose pausing them.",
    "Best creatives this week and how many fresh ones are left",
    "What needs my attention right now?",
    "How did spend and revenue move hour by hour today?",
    "Which accounts are blocked or cooling, grouped by Business Center?",
]


def _who(request: Request) -> str:
    u = getattr(request.state, "user", None)
    return (getattr(u, "email", "") or "") if u else ""


def _chats(db: Session, email: str) -> list:
    return (db.query(models.AssistantChat).filter_by(user_email=email)
            .order_by(models.AssistantChat.updated_at.desc()).limit(60).all())


@router.get("/assistant")
def page(request: Request, db: Session = Depends(get_db)):
    email = _who(request)
    chats = _chats(db, email)
    cid = request.query_params.get("chat")
    chat = None
    if cid and cid.isdigit():
        chat = db.query(models.AssistantChat).filter_by(id=int(cid), user_email=email).first()
    if chat is None and chats:
        chat = chats[0]
    blocks = asst.render_history(db, chat.id) if chat else []
    return render(request, "assistant.html", {
        "title": "Assistant", "chats": chats, "chat": chat, "blocks": blocks, "suggestions": SUGGESTIONS,
        "has_key": bool(config.ANTHROPIC_API_KEY), "model": get_settings(db)["assistant_model"],
    })


@router.post("/assistant/new")
def new_chat(request: Request, db: Session = Depends(get_db)):
    chat = models.AssistantChat(user_email=_who(request))
    db.add(chat)
    db.commit()
    return RedirectResponse(f"/assistant?chat={chat.id}", status_code=303)


@router.post("/assistant/{chat_id}/send")
async def send(request: Request, chat_id: int, db: Session = Depends(get_db)):
    chat = db.query(models.AssistantChat).filter_by(id=chat_id, user_email=_who(request)).first()
    if not chat:
        return JSONResponse({"ok": False, "error": "chat not found"}, status_code=404)
    form = await request.form()
    try:
        # the Anthropic call takes seconds — off the event loop so other users keep browsing
        res = await run_in_threadpool(asst.send, db, chat, str(form.get("text") or ""), get_settings(db)["assistant_model"])
    except asst.AssistantError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "title": chat.title, **res})


@router.post("/assistant/{chat_id}/delete")
def delete_chat(request: Request, chat_id: int, db: Session = Depends(get_db)):
    chat = db.query(models.AssistantChat).filter_by(id=chat_id, user_email=_who(request)).first()
    if chat:
        db.query(models.AssistantMessage).filter_by(chat_id=chat.id).delete()
        db.delete(chat)
        db.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True})
    return RedirectResponse("/assistant", status_code=303)
