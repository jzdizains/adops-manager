"""Signed, short-lived public links to source files — the one thing an outside
service (TensorPix image enhancement) needs from us: a URL it can fetch. Nothing is
listable; a link only works with a valid signature and before it expires."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .. import config

router = APIRouter()

ROOT = config.DATA_DIR / "creatives"


def _sig(rel: str, exp: int) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), f"{rel}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_url(base: str, abs_path: str, ttl_s: int = 2 * 3600) -> str:
    """A public URL for a file under the creatives folder, valid for ttl_s seconds."""
    from pathlib import Path
    rel = str(Path(abs_path).resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    exp = int(time.time()) + ttl_s
    return f"{base.rstrip('/')}/pub/src/{quote(rel, safe='/')}?e={exp}&s={_sig(rel, exp)}"


@router.get("/pub/src/{rel:path}")
def serve(rel: str, request: Request):
    try:
        exp = int(request.query_params.get("e") or 0)
    except ValueError:
        exp = 0
    sig = request.query_params.get("s") or ""
    if exp < time.time() or not hmac.compare_digest(sig, _sig(rel, exp)):
        return PlainTextResponse("expired or invalid link", status_code=403)
    path = (ROOT / rel).resolve()
    if ROOT.resolve() not in path.parents or not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(str(path), headers={"Cache-Control": "private, max-age=600"})
