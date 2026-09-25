"""Instant Pages kept stocked ahead of launch — web API only.

A page belongs to ONE ad account, so every account a preset can launch on needs its own copy.
Making that copy at launch time (a web-API duplicate, or worse the browser builder) put the
slow part on the launch path. With stocking on, a background job copies each missing page
from a published copy already in the workspace — through the page editor's web API
(instant_pages.clone_one: duplicate → publish → verify with /page/get/). It never starts the
browser builder.

What is stocked: every page name a preset of the workspace launches to ("offers"), on every
enabled, connected, not-suspended account of the workspace. Guard rails:
  * off until the operator turns it on (per workspace — it creates pages on real accounts);
  * at most MAX_PER_RUN copies per run, a run at most every RUN_EVERY;
  * two failures in a row pause it (something systemic — dead cookies, a TikTok change)
    until someone presses "Stock now";
  * dead cookies stop the run at once.
State lives in the Setting row "page_stock:u<user id>".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

MAX_PER_RUN = 6
RUN_EVERY = timedelta(minutes=15)
FAILURE_STREAK = 2


def _key(user_id) -> str:
    return f"page_stock:u{user_id if user_id is not None else 'all'}"


def state(db, user_id) -> dict:
    from . import queries
    try:
        st = json.loads(queries.get_setting(db, _key(user_id), "") or "{}")
    except ValueError:
        st = {}
    return {"on": bool(st.get("on")), "last_run": st.get("last_run", ""), "made": int(st.get("made") or 0),
            "streak": int(st.get("streak") or 0), "paused": st.get("paused", ""), "last": st.get("last", [])}


def save_state(db, user_id, st: dict) -> None:
    from . import queries
    queries.set_setting(db, _key(user_id), json.dumps(st))


def save_run(db, user_id, st: dict) -> dict:
    """A running pass saves only what IT changed (counters, log, pause) onto the state as it is
    NOW — so switching stocking off mid-run sticks (v151 audit). Returns the merged state."""
    cur = state(db, user_id)
    for k in ("last_run", "made", "streak", "paused", "last"):
        cur[k] = st.get(k, cur.get(k))
    save_state(db, user_id, cur)
    return cur


# ---------------------------------------------------------------------------------------
# pure pieces (tested directly)

def offer_names(presets_json: list[str]) -> list[str]:
    """Page names the presets launch to (adgroup_settings JSON blobs), first-seen order."""
    out: list[str] = []
    for raw in presets_json:
        try:
            b = json.loads(raw or "{}")
        except ValueError:
            continue
        if b.get("destination_type") != "instant_page":
            continue
        name = str(b.get("instant_page_name") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def missing_pairs(offers: list[str], account_ids: list[str], have: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """(account, page name) that still need a copy — offers outer so each page spreads evenly."""
    return [(a, name) for name in offers for a in account_ids if (a, name) not in have]


def after_result(st: dict, ok: bool, label: str, error: str = "") -> dict:
    st = dict(st)
    st["streak"] = 0 if ok else int(st.get("streak") or 0) + 1
    if ok:
        st["made"] = int(st.get("made") or 0) + 1
    last = list(st.get("last") or [])
    last.insert(0, {"at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "ok": ok, "what": label, "error": error[:200]})
    st["last"] = last[:12]
    if st["streak"] >= FAILURE_STREAK:
        st["paused"] = f"{FAILURE_STREAK} copies failed in a row — paused until “Stock now” (last: {error[:120]})"
    return st


# ---------------------------------------------------------------------------------------
# the workspace's view

def targets(db, models, user_id) -> list:
    q = db.query(models.AdAccount).filter(models.AdAccount.enabled == True)      # noqa: E712
    if user_id is not None:
        from . import scope as _scope
        q = _scope.account_filter(db, q, user_id)
    out = []
    for a in q.order_by(models.AdAccount.advertiser_name):
        st = str(a.status or "").upper()
        if a.access_token and (not st or "ENABLE" in st):
            out.append(a)
    return out


def offers(db, models, user_id) -> list[str]:
    q = db.query(models.Template.adgroup_settings)
    if user_id is not None:
        q = q.filter(models.Template.owner_user_id == user_id)
    return offer_names([r[0] for r in q])


def coverage(db, models, user_id) -> dict:
    """{offers: [{name, have, total, source}], missing, total_accounts}."""
    accts = targets(db, models, user_id)
    ids = [a.advertiser_id for a in accts]
    names = offers(db, models, user_id)
    have: set = set()
    sources: dict = {}
    if names and ids:
        for pid, owner, name, status in (db.query(models.InstantPage.page_id, models.InstantPage.owner_advertiser_id,
                                                   models.InstantPage.name, models.InstantPage.status)
                                         .filter(models.InstantPage.name.in_(names))):
            if owner in ids:
                have.add((owner, name))
                if str(status or "").upper() == "PUBLISHED":
                    sources.setdefault(name, (pid, owner))
    rows = [{"name": n, "have": sum(1 for a in ids if (a, n) in have), "total": len(ids), "can_copy": n in sources}
            for n in names]
    return {"offers": rows, "missing": missing_pairs(names, ids, have), "sources": sources, "accounts": {a.advertiser_id: a for a in accts}}


# ---------------------------------------------------------------------------------------
# running

def run(db, models, user_id, force: bool = False, should_stop=None, on_progress=None) -> dict:
    """One stocking pass for a workspace. Returns {made, failed, skipped_no_source, stopped, reason}."""
    from . import spark_web_api
    from .routes import instant_pages as ip
    st = state(db, user_id)
    if not st["on"] and not force:
        return {"made": 0, "failed": 0, "reason": "off"}
    if st["paused"] and not force:
        return {"made": 0, "failed": 0, "reason": st["paused"]}
    if force:
        st["paused"], st["streak"] = "", 0
    st["last_run"] = datetime.utcnow().isoformat(timespec="seconds")
    if not spark_web_api.load_cookies():
        st["paused"] = "No ads.tiktok.com cookies on the TikTok Cookies page — copying pages needs them."
        save_run(db, user_id, st)
        return {"made": 0, "failed": 0, "reason": st["paused"]}
    cov = coverage(db, models, user_id)
    todo = [(a, n) for a, n in cov["missing"] if n in cov["sources"]]
    made = failed = 0
    stopped = False
    for i, (adv, name) in enumerate(todo[:MAX_PER_RUN]):
        if should_stop and should_stop():
            stopped = True
            break
        if on_progress:
            on_progress(f"{i + 1} of {min(len(todo), MAX_PER_RUN)} pages")
        acct = cov["accounts"][adv]
        src_pid, src_owner = cov["sources"][name]
        label = f"“{name}” → {acct.advertiser_name or adv}"
        try:
            r = ip.clone_one(db, src_pid, name, acct, source_owner=src_owner)
        except spark_web_api.WebAuthError as e:
            st = after_result(st, False, label, f"web session dead: {e}")
            st["paused"] = "The ads.tiktok.com session is dead — refresh the cookies, then press “Stock now”."
            failed += 1
            break
        except Exception as e:      # noqa: BLE001 — one account's problem is recorded, not raised
            r = {"ok": False, "error": str(e)[:200]}
        st = after_result(st, bool(r.get("ok")), label, r.get("error", ""))
        made += bool(r.get("ok"))
        failed += not r.get("ok")
        st = save_run(db, user_id, st)
        if st.get("paused"):
            break
        if not st["on"] and not force:
            stopped = True               # switched off while this pass ran
            break
    st = save_run(db, user_id, st)
    return {"made": made, "failed": failed, "stopped": stopped, "remaining": max(len(todo) - made, 0),
            "no_source": len(cov["missing"]) - len(todo), "reason": st.get("paused", "")}


def schedule(db, models) -> int:
    """Background: queue one stocking job per workspace that has it on, isn't paused, hasn't
    run in RUN_EVERY and has something to copy. Returns how many were queued."""
    from . import jobs, queries
    n = 0
    busy = {str(json.loads(j.payload or "{}").get("user_id")) for j in db.query(models.Job).filter(
        models.Job.kind == "page_stock", models.Job.status.in_(("queued", "claimed", "running")))}
    for row in db.query(models.Setting).filter(models.Setting.key.like("page_stock:u%")):
        try:
            st = json.loads(row.value or "{}")
        except ValueError:
            continue
        if not st.get("on") or st.get("paused"):
            continue
        uid_raw = row.key.split(":u", 1)[1]
        uid = None if uid_raw == "all" else int(uid_raw) if uid_raw.isdigit() else None
        if str(uid) in busy:
            continue
        try:
            last = datetime.fromisoformat(st.get("last_run") or "2000-01-01")
        except ValueError:
            last = datetime(2000, 1, 1)
        if datetime.utcnow() - last < RUN_EVERY:
            continue
        cov = coverage(db, models, uid)
        if not any(n_ in cov["sources"] for _, n_ in cov["missing"]):
            continue
        jobs.enqueue(db, "page_stock", "Stock Instant Pages on every account", {"user_id": uid}, href="/instant-pages", quiet=True)
        n += 1
    return n
