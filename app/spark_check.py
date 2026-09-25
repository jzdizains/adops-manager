"""Spark codes checked the moment they're pasted — not when a launch fails on them.

A pasted code used to be stored blind: a wrong, expired or revoked code stayed "fresh" until a
launch died on it, and the post behind it (its id, photo-or-video, cover) was only learnt then.
Now each new code is authorised on ONE anchor account of its workspace and looked up:

  * TikTok's post id, the real media type (photo vs video), the post link and the cover are
    filled in (the cover copied while its URL is alive — thumbs.py);
  * a code TikTok calls wrong / expired is marked `bad` straight away, with TikTok's words;
  * the same post pasted twice is flagged as a duplicate of the first;
  * anything else that fails (rate limit, TikTok still indexing a new post, a blip) is retried
    on a ladder — 30 s, 2 min, 5 min — and only then marked `error` ("couldn't check").

States (SparkCode.check_state): "" never checked (codes from before v147) · checking · ok ·
bad · error. A launch never waits for this, and nothing is blocked by it: the picker shows it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

RETRY_DELAYS_S = (30, 120, 300)
CLAIM_S = 600                     # a row being checked is hidden from the other worker this long
BAD_CODE_RE = re.compile(r"code is (incorrect|invalid|wrong)|invalid (auth|post) ?code|code (has )?(expired|been revoked)|"
                         r"(post|video|item|spark) (authori[sz]ation )?(has )?(expired|been revoked)|"
                         r"authori[sz]ation (has )?(expired|been revoked|been cancel|been turned off|cancel|revok|turned off)", re.I)
# the ANCHOR account's own access (its token / permissions) — not the code's fault: try again later
ACCESS_RE = re.compile(r"access[_ ]?token|refresh[_ ]?token", re.I)
TRANSIENT_RE = re.compile(r"internal error|try again|try later|please retry|system busy|timeout|timed out|rate.?limit|\brate\b|"
                          r"too many|frequen|not (yet )?(indexed|found|ready)|does not exist|processing", re.I)


def _now():
    return datetime.utcnow()


# ---------------------------------------------------------------------------------------
# pure pieces (tested directly)

def classify(code, message: str) -> str:
    """'bad' (the code itself is refused) | 'transient' (worth another try) | 'error'."""
    msg = str(message or "")
    if ACCESS_RE.search(msg):
        return "transient"               # a lapsed anchor token must never brand good codes "bad"
    if BAD_CODE_RE.search(msg):
        return "bad"
    if str(code) in ("40100", "50000", "51000", "HTTP") or TRANSIENT_RE.search(msg):
        return "transient"
    return "error"


def next_step(attempt: int, kind: str) -> tuple[str, int | None]:
    """(state, seconds until the next try) after a failed attempt number `attempt` (1-based)."""
    if kind == "bad":
        return "bad", None
    if kind == "transient" and attempt <= len(RETRY_DELAYS_S):
        return "checking", RETRY_DELAYS_S[attempt - 1]
    return "error", None


def _first(d: dict, *keys) -> str:
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, (str, int)) and str(v):
            return str(v)
    return ""


def post_facts(*answers) -> dict:
    """item_id, media_type, cover, url, caption from any mix of /tt_video/authorize/ and
    /tt_video/info/ answers (the shapes vary: flat, item_info / video_info, or a list)."""
    nodes: list[dict] = []
    for a in answers:
        if not isinstance(a, dict):
            continue
        nodes.append(a)
        for k in ("item_info", "video_info", "data"):
            if isinstance(a.get(k), dict):
                nodes.append(a[k])
        for k in ("list", "item_list", "video_list"):
            v = a.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                nodes.append(v[0])
                for kk in ("item_info", "video_info"):
                    if isinstance(v[0].get(kk), dict):
                        nodes.append(v[0][kk])
    out = {"item_id": "", "media_type": "", "cover": "", "url": "", "caption": ""}
    for n in nodes:
        out["item_id"] = out["item_id"] or _first(n, "item_id", "tiktok_item_id")
        t = str(n.get("item_type") or n.get("media_type") or "").upper()
        if not out["media_type"] and t:
            out["media_type"] = "CAROUSEL" if ("CAROUSEL" in t or "PHOTO" in t or "IMAGE" in t) else ("VIDEO" if "VIDEO" in t else "")
        out["cover"] = out["cover"] or _first(n, "poster_url", "video_cover_url", "cover_url", "cover_image_url", "thumbnail_url")
        out["url"] = out["url"] or _first(n, "share_url", "item_url", "post_url")
        out["caption"] = out["caption"] or _first(n, "text", "caption", "title")
        imgs = (n.get("carousel_info") or {}).get("image_info") if isinstance(n.get("carousel_info"), dict) else None
        if not out["cover"] and isinstance(imgs, list) and imgs and isinstance(imgs[0], dict):
            out["cover"] = _first(imgs[0], "image_url", "url")
    return out


# ---------------------------------------------------------------------------------------
# the check

def anchor_account(db, models, owner_user_id):
    """An enabled, connected, not-suspended account of the code's workspace to ask through."""
    q = (db.query(models.AdAccount).filter(models.AdAccount.enabled == True,           # noqa: E712
                                           models.AdAccount.access_token != "")
         .order_by(models.AdAccount.advertiser_id))
    if owner_user_id is not None:
        from . import scope as _scope
        q = _scope.account_filter(db, q, owner_user_id)
    for a in q.limit(50):
        st = str(a.status or "").upper()
        if not st or "ENABLE" in st:
            return a
    return None


def check_one(db, models, spark, tiktok_api, anchor=None) -> str:
    """Authorise + look up one code. Writes the outcome on the row (no commit). Returns the state."""
    code = (spark.code or "").strip()
    spark.check_attempts = int(spark.check_attempts or 0) + 1
    if not code:
        spark.check_state, spark.check_error, spark.check_next_at = ("ok" if spark.tiktok_item_id else "error"), \
            ("" if spark.tiktok_item_id else "No auth code to check."), None
        spark.checked_at = _now()
        return spark.check_state
    acct = anchor or anchor_account(db, models, spark.owner_user_id)
    if acct is None:
        spark.check_state, spark.check_error, spark.check_next_at = "error", "No connected ad account to check it through.", None
        return "error"
    authz, info, err = {}, {}, None
    try:
        authz = tiktok_api.authorize_tt_video(acct.access_token, acct.advertiser_id, code) or {}
    except tiktok_api.TikTokError as e:
        if classify(e.code, e.message) == "bad":
            err = e
        # anything else: an already-authorised code answers with an error too — read the post anyway
    if err is None:
        try:
            info = tiktok_api.tt_video_info(acct.access_token, acct.advertiser_id, auth_code=code) or {}
        except tiktok_api.TikTokError as e:
            err = e
    facts = post_facts(authz, info)
    if err is not None and facts["item_id"] and classify(getattr(err, "code", ""), getattr(err, "message", "")) != "bad":
        err = None                        # the authorize answer already named the post
    if err is None and not facts["item_id"]:
        err = tiktok_api.TikTokError("APP", "TikTok didn't return the post yet (not indexed)")
    if err is not None:
        state, delay = next_step(spark.check_attempts, classify(getattr(err, "code", ""), getattr(err, "message", str(err))))
        spark.check_state = state
        spark.check_error = (getattr(err, "message", "") or str(err))[:300]
        spark.check_next_at = (_now() + timedelta(seconds=delay)) if delay else None
        if state != "checking":
            spark.checked_at = _now()
        return state
    # ---- the post is known ----
    spark.tiktok_item_id = spark.tiktok_item_id or facts["item_id"]
    if facts["media_type"]:
        spark.media_type = facts["media_type"]                   # TikTok's word beats the form's
    if facts["url"] and not spark.tiktok_post_url:
        spark.tiktok_post_url = facts["url"]
    if facts["caption"] and (not spark.name or spark.name == code[:12]):
        spark.name = facts["caption"][:80]
    import contextlib
    # no autoflush: the row's pending changes must not open a write transaction that the
    # cover download below would then hold across the network
    with (getattr(db, "no_autoflush", None) or contextlib.nullcontext()):
        dup = (db.query(models.SparkCode).filter(models.SparkCode.tiktok_item_id == spark.tiktok_item_id,
                                                 models.SparkCode.id != spark.id,
                                                 models.SparkCode.owner_user_id == spark.owner_user_id)
               .order_by(models.SparkCode.id).first())
    spark.check_state, spark.check_next_at, spark.checked_at = "ok", None, _now()
    spark.check_error = f"Same post as “{dup.name or dup.code[:12]}” (#{dup.id})." if dup is not None else ""
    if facts["cover"]:
        try:
            from . import thumbs
            if not thumbs.cache_spark(db, spark, facts["cover"]) and not spark.thumbnail_url:
                spark.thumbnail_url = facts["cover"] if thumbs.allowed(facts["cover"]) else ""
        except Exception:      # noqa: BLE001
            pass
    return "ok"


def _claim(db, models, ids: list[int] | None, limit: int) -> list:
    """Rows due for a check, marked taken (committed) so the sweep and a job never both do one."""
    now = _now()
    q = db.query(models.SparkCode).filter(models.SparkCode.check_state == "checking")
    if ids:
        q = q.filter(models.SparkCode.id.in_(ids))
    # never one another worker has just taken (a re-check resets check_next_at before kicking)
    q = q.filter((models.SparkCode.check_next_at == None) | (models.SparkCode.check_next_at <= now))   # noqa: E711
    rows = q.order_by(models.SparkCode.id).limit(limit).all()
    for r in rows:
        r.check_next_at = now + timedelta(seconds=CLAIM_S)
    if rows:
        db.commit()
    return rows


def run(db, models, tiktok_api, ids: list[int] | None = None, limit: int = 25) -> dict:
    """Check the given (or all due) codes. The network calls happen with nothing uncommitted."""
    import time as _t
    rows = _claim(db, models, ids, limit)
    out = {"ok": 0, "bad": 0, "error": 0, "checking": 0}
    anchors: dict = {}
    for r in rows:
        if r.owner_user_id not in anchors:
            anchors[r.owner_user_id] = anchor_account(db, models, r.owner_user_id)
        st = check_one(db, models, r, tiktok_api, anchors[r.owner_user_id])
        out[st] = out.get(st, 0) + 1
        db.commit()
        _t.sleep(0.2)
    return out


def kick(db, ids: list[int]) -> None:
    """New codes: mark them and check them now in the background."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return
    try:
        from . import jobs
        jobs.enqueue(db, "spark_check", f"Check {len(ids)} spark code(s)", {"ids": ids[:500]}, quiet=True)
    except Exception:      # noqa: BLE001 — the sweep picks them up anyway
        pass


def view(spark) -> dict:
    return {"state": getattr(spark, "check_state", "") or "", "error": getattr(spark, "check_error", "") or ""}
