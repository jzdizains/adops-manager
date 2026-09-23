"""/lab — the Testing Lab (lab.py): boards of experiments tied to launch batches.
Everything but the two pages is fetch/JSON so the board edits in place (drawers, no reloads)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import lab, models, scope as scope_mod
from ..database import get_db
from ..templating import render

router = APIRouter()


def _board(db: Session, sc, board_id: int):
    b = db.get(models.LabBoard, int(board_id))
    return b if (b is not None and sc.owns(b)) else None


def _test(db: Session, sc, test_id: int):
    t = db.get(models.LabTest, int(test_id))
    if t is None:
        return None, None
    b = _board(db, sc, t.board_id)
    return (t, b) if b is not None else (None, None)


@router.get("/lab")
def lab_home(request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    show_arch = request.query_params.get("archived") == "1"
    boards = (sc.owned(db.query(models.LabBoard), models.LabBoard).filter(models.LabBoard.archived == show_arch)
              .order_by(models.LabBoard.updated_at.desc()).all())
    counts = {}
    for bid, st in db.query(models.LabTest.board_id, models.LabTest.status).filter(models.LabTest.board_id.in_([b.id for b in boards] or [0])):
        counts.setdefault(bid, {}).setdefault(st, 0)
        counts[bid][st] += 1
    return render(request, "lab.html", {"title": "Lab", "boards": boards, "counts": counts, "show_arch": show_arch,
                                        "labels": lab.STATUS_LABEL})


@router.get("/lab/{board_id}")
def lab_board(board_id: int, request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    b = _board(db, sc, board_id)
    if b is None:
        return RedirectResponse("/lab", status_code=303)
    payload = lab.board_payload(db, models, b, sc)
    recent = [r[0] for r in db.query(models.LaunchLog.batch_ref, models.LaunchLog.advertiser_id).order_by(models.LaunchLog.id.desc()).limit(400)
              if r[0] and sc.allows(r[1])]
    return render(request, "lab_board.html", {"title": f"Lab · {b.title}", "board": payload, "statuses": lab.STATUSES,
                                              "labels": lab.STATUS_LABEL, "recent_refs": list(dict.fromkeys(recent))[:30]})


@router.post("/lab/boards")
async def board_save(request: Request, db: Session = Depends(get_db)):
    """Create (no id) or edit a board. Fields: id, title, offer, description, archived."""
    sc = scope_mod.for_request(request, db)
    f = await request.form()
    title = str(f.get("title") or "").strip()[:200]
    bid = str(f.get("id") or "")
    if bid.isdigit():
        b = _board(db, sc, int(bid))
        if b is None:
            return JSONResponse({"ok": False, "error": "That board is gone."}, status_code=404)
    else:
        if not title:
            return JSONResponse({"ok": False, "error": "Give the board a title."})
        b = models.LabBoard(owner_user_id=sc.owner_for_new)
        db.add(b)
    if title:
        b.title = title
    for k in ("offer", "description"):
        if k in f:
            setattr(b, k, str(f.get(k) or "").strip()[:5000 if k == "description" else 200])
    if "archived" in f:
        b.archived = str(f.get("archived")) == "1"
    lab.touch(b)
    db.commit()
    return JSONResponse({"ok": True, "id": b.id, "href": f"/lab/{b.id}"})


@router.post("/lab/tests")
async def test_save(request: Request, db: Session = Depends(get_db)):
    """Create (board_id, no id) or edit a test. Fields: title, hypothesis, variable, status,
    batch_refs (refs or pasted result links), learning, parent_id."""
    sc = scope_mod.for_request(request, db)
    f = await request.form()
    tid = str(f.get("id") or "")
    if tid.isdigit():
        t, b = _test(db, sc, int(tid))
        if t is None:
            return JSONResponse({"ok": False, "error": "That test is gone."}, status_code=404)
    else:
        b = _board(db, sc, int(f.get("board_id") or 0)) if str(f.get("board_id") or "").isdigit() else None
        if b is None:
            return JSONResponse({"ok": False, "error": "Pick a board."}, status_code=404)
        n = db.query(models.LabTest).filter(models.LabTest.board_id == b.id).count()
        t = models.LabTest(board_id=b.id, position=n, title=str(f.get("title") or "New test").strip()[:200] or "New test")
        db.add(t)
    for k, cap in (("title", 200), ("hypothesis", 3000), ("variable", 120), ("learning", 5000)):
        if k in f:
            setattr(t, k, str(f.get(k) or "").strip()[:cap])
    if "status" in f and str(f.get("status")) in lab.STATUSES:
        t.status = str(f.get("status"))
    if "batch_refs" in f:
        t.batch_refs = ", ".join(lab.refs(str(f.get("batch_refs") or "")))
    if "parent_id" in f:
        p = str(f.get("parent_id") or "")
        t.parent_id = int(p) if p.isdigit() and int(p) != (t.id or 0) else None
    lab.touch(t)
    lab.touch(b)
    db.commit()
    return JSONResponse({"ok": True, "id": t.id})


@router.post("/lab/tests/{test_id}/delete")
def test_delete(test_id: int, request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    t, b = _test(db, sc, test_id)
    if t is None:
        return JSONResponse({"ok": False, "error": "That test is gone."}, status_code=404)
    for child in db.query(models.LabTest).filter(models.LabTest.parent_id == t.id):
        child.parent_id = t.parent_id
    db.delete(t)
    lab.touch(b)
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/lab/{board_id}/data.json")
def board_json(board_id: int, request: Request, db: Session = Depends(get_db)):
    sc = scope_mod.for_request(request, db)
    b = _board(db, sc, board_id)
    if b is None:
        return JSONResponse({"ok": False}, status_code=404)
    return JSONResponse({"ok": True, **lab.board_payload(db, models, b, sc)})
