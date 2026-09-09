"""Spark codes list CRUD + the Spark Hub (auto-grab from connected creators).

Auto-grab (§5): list_identities → for each creator identity, list_tt_videos
(returns only AD-AUTHORIZED posts — §9.4) → store each item's auth_code,
item_id, media type, thumbnail and post link as SparkCode rows grouped by
creator. Hand-entered codes leave tiktok_item_id empty.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, queries, tiktok_api
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/spark-codes")
def spark_list(request: Request, db: Session = Depends(get_db)):
    """One table for every spark, with what it earned: tests (launches), spend,
    revenue, profit and ROAS all-time, plus creator and status filters."""
    from sqlalchemy import func as _f
    from .. import pnl_data, timeutil
    q = request.query_params.get("q", "").strip().lower()
    mine_only = request.query_params.get("mine", "") == "1"
    state = request.query_params.get("state", "all")
    creator = request.query_params.get("creator", "").strip()
    groups = {g.id: g for g in db.query(models.SparkCodeGroup).all()}
    my_creators = {s.creator_handle for s in db.query(models.SparkSetting).filter_by(is_mine=True).all()}
    codes = db.query(models.SparkCode).order_by(models.SparkCode.created_at.desc()).all()
    # launches + P&L per spark (all time, DB only)
    logs = db.query(models.LaunchLog).filter(models.LaunchLog.spark_code_id.isnot(None), models.LaunchLog.ok == True).all()   # noqa: E712
    by_spark: dict[int, list] = {}
    for lg in logs:
        by_spark.setdefault(lg.spark_code_id, []).append(lg)
    cids = [lg.campaign_id for lg in logs if lg.campaign_id]
    spend_by_cid = {c: float(v or 0) for c, v in db.query(models.SpendSnapshot.campaign_id, _f.sum(models.SpendSnapshot.spend))
                    .filter(models.SpendSnapshot.campaign_id.in_(cids)).group_by(models.SpendSnapshot.campaign_id)} if cids else {}
    src_map = pnl_data.campaign_source_map(db)
    pb = pnl_data.revenue_by_source(db, timeutil.local_midnight_utc(-365), timeutil.local_midnight_utc(1))
    camp_names = {c.campaign_id: c for c in db.query(models.CampaignRecord).filter(models.CampaignRecord.campaign_id.in_(cids)).all()} if cids else {}
    rows = []
    creators: dict[str, int] = {}
    for c in codes:
        g = groups.get(c.group_id) if c.group_id else None
        cname = g.name if g else ""
        if cname:
            creators[cname] = creators.get(cname, 0) + 1
        lgs = by_spark.get(c.id, [])
        spend = sum(spend_by_cid.get(lg.campaign_id, 0.0) for lg in lgs)
        srcs = {src_map.get(lg.campaign_id, "") or (lg.source or "") for lg in lgs} - {""}
        revenue = sum(float(pb.get(sx, {}).get("revenue", 0.0)) for sx in srcs)
        live = sum(1 for lg in lgs if lg.campaign_id in camp_names and camp_names[lg.campaign_id].operation_status == "ENABLE")
        rows.append({"c": c, "creator": cname, "mine": cname in my_creators, "tests": len(lgs), "live": live, "spend": spend, "revenue": revenue,
                     "profit": revenue - spend, "roas": (revenue / spend) if spend else 0.0, "has": bool(srcs)})
    def _ok(r):
        c = r["c"]
        if mine_only and my_creators and r["creator"] not in my_creators:
            return False
        if creator and r["creator"] != creator:
            return False
        if state == "active" and c.status != "active":
            return False
        if state == "used" and not c.use_count:
            return False
        if state == "unused" and c.use_count:
            return False
        if q and q not in (c.name or "").lower() and q not in (c.code or "").lower() and q not in r["creator"].lower() and q not in (c.source or "").lower():
            return False
        return True
    shown = [r for r in rows if _ok(r)]
    shown.sort(key=lambda r: (r["profit"], r["c"].created_at or 0), reverse=True)
    counts = {"all": len(rows), "active": sum(1 for r in rows if r["c"].status == "active"), "used": sum(1 for r in rows if r["c"].use_count),
              "unused": sum(1 for r in rows if not r["c"].use_count)}
    return render(request, "spark_codes.html", {
        "rows": shown, "counts": counts, "creators": sorted(creators.items(), key=lambda kv: (-kv[1], kv[0])), "creator": creator,
        "q": q, "mine_only": mine_only, "state": state, "my_creators": my_creators, "title": "Sparks",
        "tot": {"spend": sum(r["spend"] for r in shown), "profit": sum(r["profit"] for r in shown), "tests": sum(r["tests"] for r in shown)},
        "ok": request.query_params.get("ok", ""), "err": request.query_params.get("err", ""),
    })


@router.get("/spark-codes/pick.json")
def pick_json(request: Request, db: Session = Depends(get_db)):
    """The spark picker's data (Super Launcher / Single campaign): every code with its
    creator, type, status and post link. state = fresh (active) | used | all; q = search;
    id = one specific code (to show a pre-selected one)."""
    from fastapi.responses import JSONResponse
    from ..templating import _ago
    qp = request.query_params
    state = qp.get("state") or "fresh"
    q = (qp.get("q") or "").strip().lower()
    only_id = qp.get("id")
    query = db.query(models.SparkCode).order_by(models.SparkCode.created_at.desc())
    if only_id and only_id.isdigit():
        query = query.filter(models.SparkCode.id == int(only_id))
    elif state == "fresh":
        query = query.filter(models.SparkCode.status == "active")
    elif state == "used":
        query = query.filter(models.SparkCode.status != "active")
    items = []
    for s in query.limit(500):
        creator = s.group.name if s.group else ""
        hay = f"{s.name} {s.code} {creator} {s.source}".lower()
        if q and q not in hay:
            continue
        items.append({"id": s.id, "name": s.name or s.code[:16], "code": s.code, "creator": creator, "type": (s.media_type or "VIDEO").lower(),
                      "state": "fresh" if s.status == "active" else (s.status or "used"), "thumb": s.thumbnail_url or "",
                      "post_url": s.tiktok_post_url or "", "source": s.source or "", "uses": int(s.use_count or 0),
                      "last_used": _ago(s.last_used_at) if s.last_used_at else "", "added": _ago(s.created_at) if s.created_at else ""})
    counts = {"fresh": db.query(models.SparkCode).filter_by(status="active").count(),
              "used": db.query(models.SparkCode).filter(models.SparkCode.status != "active").count()}
    return JSONResponse({"items": items, "counts": counts})


@router.post("/spark-codes/add")
def add_code(name: str = Form(""), code: str = Form(...), media_type: str = Form("VIDEO"),
             tiktok_post_url: str = Form(""), group_name: str = Form(""),
             source: str = Form(""), db: Session = Depends(get_db)):
    group = None
    if group_name.strip():
        group = db.query(models.SparkCodeGroup).filter_by(name=group_name.strip()).first()
        if not group:
            group = models.SparkCodeGroup(name=group_name.strip())
            db.add(group)
            db.flush()
    db.add(models.SparkCode(name=name.strip(), code=code.strip(), media_type=media_type,
                            tiktok_post_url=tiktok_post_url.strip(), source=source.strip(),
                            group_id=group.id if group else None))
    db.commit()
    return RedirectResponse("/spark-codes?ok=added", status_code=303)


# ---------------------------------------------------------------------------
# bulk add: one code per line (paste) or a CSV/TSV file
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"^#?[A-Za-z0-9_\-+/=]{12,}$")     # spark auth codes: long token, often '#…=' or 'CT7Q…'
_URL_RE = re.compile(r"^https?://", re.I)
_MEDIA = {"VIDEO": "VIDEO", "V": "VIDEO", "CAROUSEL": "CAROUSEL", "C": "CAROUSEL", "PHOTO": "CAROUSEL",
          "PHOTOS": "CAROUSEL", "SLIDES": "CAROUSEL", "SLIDE": "CAROUSEL", "IMAGE": "CAROUSEL", "IMAGES": "CAROUSEL"}
_HEADERS = {"name", "code", "auth_code", "spark", "spark_code", "media_type", "type", "url", "post_url",
            "tiktok_post_url", "source", "group", "creator", "group_name"}


def _split_line(line: str) -> list[str]:
    """Cells separated by | ; tab or comma (whichever the line uses)."""
    for sep in ("\t", "|", ";"):
        if sep in line:
            return [c.strip() for c in line.split(sep)]
    if "," in line:
        return [c.strip() for c in line.split(",")]
    return [line.strip()]


def parse_bulk_line(line: str, default_media: str = "VIDEO") -> dict | None:
    """One line → {name, code, media_type, tiktok_post_url, source, group_name}
    or None when no code can be found. Cells may come in any order: the
    auth code is the long token (or the one starting with '#'), a URL is the
    post URL, VIDEO/CAROUSEL (or photo/slides) is the type, an '@handle' is
    the creator, the first other cell is the name, the next is the source."""
    cells = [c for c in _split_line(line) if c]
    if not cells:
        return None
    out = {"name": "", "code": "", "media_type": default_media, "tiktok_post_url": "", "source": "", "group_name": ""}
    rest: list[str] = []
    for c in cells:
        u = c.upper()
        if not out["tiktok_post_url"] and _URL_RE.match(c):
            out["tiktok_post_url"] = c
        elif u in _MEDIA:
            out["media_type"] = _MEDIA[u]
        elif not out["group_name"] and c.startswith("@") and len(c) > 1:
            out["group_name"] = c.lstrip("@")
        elif not out["code"] and (c.startswith("#") or (_CODE_RE.match(c) and " " not in c and len(c) >= 16)):
            out["code"] = c
        else:
            rest.append(c)
    if not out["code"]:
        # a lone long token without '#' but with no spaces still counts
        for c in list(rest):
            if _CODE_RE.match(c) and len(c) >= 12:
                out["code"] = c
                rest.remove(c)
                break
    if not out["code"]:
        return None
    if rest:
        out["name"] = rest[0][:120]
    if len(rest) > 1:
        out["source"] = rest[1][:120]
    if "/photo/" in out["tiktok_post_url"] and "media_type" not in [x.upper() for x in cells if x.upper() in _MEDIA]:
        out["media_type"] = "CAROUSEL"      # TikTok photo-post URLs are carousels
    return out


def parse_bulk(text: str, default_media: str = "VIDEO") -> tuple[list[dict], list[str]]:
    """Every non-empty line; a header line (name, code, …) is skipped.
    Returns (rows, unreadable_lines)."""
    rows, bad = [], []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        cells = {c.strip().lower() for c in _split_line(line)}
        if cells and cells <= _HEADERS:
            continue              # CSV header
        parsed = parse_bulk_line(line, default_media)
        if parsed:
            rows.append(parsed)
        else:
            bad.append(line[:80])
    return rows, bad


@router.post("/spark-codes/bulk")
async def add_bulk(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    text = str(form.get("lines") or "")
    upload = form.get("file")
    if upload is not None and getattr(upload, "filename", ""):
        try:
            text += "\n" + (await upload.read()).decode("utf-8-sig", errors="replace")
        except Exception:  # noqa: BLE001
            return RedirectResponse("/spark-codes?err=" + quote("Could not read that file — paste the lines instead."), status_code=303)
    default_media = "CAROUSEL" if str(form.get("media_type") or "").upper() == "CAROUSEL" else "VIDEO"
    default_group = str(form.get("group_name") or "").strip().lstrip("@")
    default_source = str(form.get("source") or "").strip()
    rows, bad = parse_bulk(text, default_media)
    if not rows:
        return RedirectResponse("/spark-codes?err=" + quote(
            "No codes found. One per line — the auth code alone, or  name | code | video/carousel | post URL | source."), status_code=303)
    existing = {c.code for c in db.query(models.SparkCode.code).all()}
    groups: dict[str, models.SparkCodeGroup] = {}

    def group_for(name: str):
        if not name:
            return None
        g = groups.get(name.lower())
        if not g:
            g = db.query(models.SparkCodeGroup).filter(func.lower(models.SparkCodeGroup.name) == name.lower()).first()
            if not g:
                g = models.SparkCodeGroup(name=name)
                db.add(g)
                db.flush()
            groups[name.lower()] = g
        return g

    added, dupes, seen = 0, 0, set()
    for r in rows:
        if r["code"] in existing or r["code"] in seen:
            dupes += 1
            continue
        seen.add(r["code"])
        g = group_for(r["group_name"] or default_group)
        db.add(models.SparkCode(name=r["name"] or r["code"][:12], code=r["code"], media_type=r["media_type"],
                                tiktok_post_url=r["tiktok_post_url"], source=r["source"] or default_source,
                                group_id=g.id if g else None))
        added += 1
    db.commit()
    msg = f"added {added} spark code(s)"
    if dupes:
        msg += f", {dupes} already existed"
    if bad:
        msg += f", {len(bad)} line(s) had no code (" + "; ".join(bad[:3]) + ("…" if len(bad) > 3 else "") + ")"
    return RedirectResponse(("/spark-codes?ok=" if added else "/spark-codes?err=") + quote(msg), status_code=303)


@router.post("/spark-codes/{code_id}/source")
def set_source(code_id: int, source: str = Form(""), db: Session = Depends(get_db)):
    """Set/change the source on a spark code (P&L join key)."""
    c = db.get(models.SparkCode, code_id)
    if c:
        c.source = source.strip()
        db.commit()
    return RedirectResponse("/spark-codes?ok=source+saved", status_code=303)


@router.post("/spark-codes/{code_id}/update")
def update_code(code_id: int, name: str = Form(""), source: str = Form(""),
                code: str = Form(""), db: Session = Depends(get_db)):
    """Edit a spark code row: name + source anytime; the pasted code itself only
    while the spark has never launched (editing it after would break history)."""
    c = db.get(models.SparkCode, code_id)
    if not c:
        return RedirectResponse("/spark-codes?err=not+found", status_code=303)
    c.name = name.strip()[:120]
    if (c.source or "").strip() != source.strip() and (c.use_count or 0) > 0 and (c.source or "").strip():
        return RedirectResponse(
            "/spark-codes?err=source+is+locked+after+the+spark+has+launched+(P%26L+history)",
            status_code=303)
    c.source = source.strip()
    new_code = code.strip()
    if new_code and new_code != (c.code or ""):
        if (c.use_count or 0) > 0:
            return RedirectResponse(
                "/spark-codes?err=code+is+locked+after+the+spark+has+launched",
                status_code=303)
        c.code = new_code
    db.commit()
    return RedirectResponse("/spark-codes?ok=saved", status_code=303)


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
            from .campaigns import _account_identities
            identities = _account_identities(acct)   # includes BC_AUTH_TT (needs bc id)
        except tiktok_api.TikTokError as e:
            errors.append(f"{acct.advertiser_id}: {e.code}")
            continue
        for ident in identities:
            itype = ident.get("identity_type", "")
            if itype not in ("TT_USER", "BC_AUTH_TT"):
                continue  # only real creator identities carry grabbable posts
            handle = ident.get("display_name", "") or ident.get("identity_id", "")
            try:
                data = tiktok_api.list_tt_videos(
                    acct.access_token, acct.advertiser_id,
                    ident["identity_id"], itype,
                    identity_authorized_bc_id=(acct.owner_bc_id or "") if itype == "BC_AUTH_TT" else "")
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
