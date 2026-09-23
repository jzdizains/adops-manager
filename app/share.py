"""Share (v155.9): one failed launch as a paste-ready text report — what the operator pastes into
a chat or a support ticket instead of screenshots. Everything in it is already on the result page
or in Diagnostics; tokens and secrets never are (diag contexts are redacted when stored).

  launch_report(...)  — pure: the text, from plain values
  find_request(...)   — the request TikTok refused, from Diagnostics: matched by TikTok's
                        request_id, else by the same message on the same account around that time
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

_RID = re.compile(r"request_id=([0-9A-Za-z]+)")
_MSG = re.compile(r"message=(.*?)(?: request_id=| at=|$)")
MAX_BODY = 3500


def request_id_of(technical: str) -> str:
    m = _RID.search(technical or "")
    return m.group(1) if m else ""


def find_request(db, models, log) -> dict | None:
    """{where, code, message, body, request_id} of the refused call, or None."""
    rid = request_id_of(getattr(log, "error_technical", "") or "")
    row = None
    if rid:
        row = db.query(models.DiagEvent).filter(models.DiagEvent.request_id == rid).order_by(models.DiagEvent.id.desc()).first()
    if row is None and getattr(log, "created_at", None):
        m = _MSG.search(getattr(log, "error_technical", "") or "")
        msg = (m.group(1).strip() if m else "")[:120]
        if msg:
            t0 = log.created_at - timedelta(minutes=2)
            t1 = log.created_at + timedelta(minutes=30)
            for r in (db.query(models.DiagEvent).filter(models.DiagEvent.kind == "tiktok", models.DiagEvent.last_at >= t0,
                                                        models.DiagEvent.first_at <= t1)
                      .order_by(models.DiagEvent.id.desc()).limit(50)):
                if msg in (r.message or "") and str(log.advertiser_id) in (r.context or ""):
                    row = r
                    break
    if row is None:
        return None
    try:
        ctx = json.loads(row.context or "{}")
    except ValueError:
        ctx = {}
    return {"where": row.where or "", "code": row.code or "", "message": row.message or "",
            "request_id": row.request_id or rid, "body": (ctx or {}).get("body", ctx)}


def launch_report(*, preset: str, account: str, advertiser_id: str, campaign_id: str, batch_ref: str,
                  friendly: str, technical: str, steps: list | None, request: dict | None, when: str = "") -> str:
    """The text. Pure."""
    out = [f"FAILED LAUNCH — preset “{preset or '?'}” on {account or advertiser_id} ({advertiser_id})",
           f"batch {batch_ref}" + (f" · campaign {campaign_id}" if campaign_id else "") + (f" · {when}" if when else ""),
           "", "What the dashboard says:", "  " + (friendly or "—").strip(),
           "", "TikTok's answer:", "  " + (technical or "—").strip()]
    if steps:
        out += ["", "Steps:"] + [f"  {i + 1}. {s.get('t', '')} {s.get('s', '')}".rstrip() for i, s in enumerate(steps[-12:])]
    if request:
        body = json.dumps(request.get("body") or {}, ensure_ascii=False, indent=1)
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY] + "\n  … (cut)"
        out += ["", f"The request TikTok refused ({request.get('where', '')}, code {request.get('code', '')}):", body]
    else:
        out += ["", "The refused request isn't in Diagnostics (older than its window, or not a TikTok call)."]
    return "\n".join(out)
