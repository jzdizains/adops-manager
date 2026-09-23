"""A launch's record, written BEFORE the first TikTok create and after every step.

The launch log used to be written once, at the very end. A restart mid-launch (a deploy, an
out-of-memory kill) left no trace of the campaign it had just created, so the account looked
"never launched" and the next try put a second campaign on it. Now every account's launch
opens a trace row first, marks each create call as in flight before it is sent, and records
what came back (campaign id, each ad group id, how many ads it got). That gives:

  * recovery — at boot, a trace still "running" belongs to a launch the restart killed; it
    becomes a failed launch-log row that says exactly what exists, so nothing relaunches it
    blindly (relaunch_safe reads the campaign id / the may-have-been-created mark);
  * resume — "Retry failed" on a half-built account continues INSIDE the existing campaign:
    the ad groups that already have ads are skipped, empty ones are deleted, the rest built;
  * the result page's per-account step list.

Every write here is best-effort: a trace problem never fails the launch itself.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

MAX_STEPS = 60
STALE_AFTER = timedelta(hours=2)        # a live launch updates its trace every few seconds


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load(raw, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except (TypeError, ValueError):
        return default


def bound_steps(steps: list, cap: int = MAX_STEPS) -> list:
    """Keep the first few steps (what the launch set out to do) and the latest ones."""
    if len(steps) <= cap:
        return steps
    head = 8
    return steps[:head] + [{"t": steps[head].get("t", ""), "s": f"… {len(steps) - cap + 1} steps omitted"}] + steps[-(cap - head - 1):]


class Trace:
    """Wraps one LaunchTrace row. All methods swallow their own errors."""

    def __init__(self, db, models, batch_ref: str, advertiser_id: str, fields: dict):
        self.db, self.models, self.row = db, models, None
        self.steps: list = []
        self.adgroups: list = []
        try:
            self.row = models.LaunchTrace(
                batch_ref=batch_ref or "", advertiser_id=str(advertiser_id), status="running",
                smart_plus=bool(fields.get("smart_plus")),
                resumed=bool((fields.get("_resume_by_account") or {}).get(str(advertiser_id))))
            db.add(self.row)
            self.step("started")
            self.save()
        except Exception:      # noqa: BLE001
            self.row = None

    # -- low level -------------------------------------------------------------------------
    def step(self, text: str) -> None:
        self.steps.append({"t": _now().strftime("%H:%M:%S"), "s": str(text)[:200]})

    def save(self) -> None:
        if self.row is None:
            return
        try:
            self.row.steps = json.dumps(bound_steps(self.steps))
            self.row.adgroups = json.dumps(self.adgroups)
            self.row.updated_at = _now()
            self.db.add(self.row)          # (re-attaches it if a busy-rollback dropped it)
            from .database import safe_commit
            safe_commit(self.db)
        except Exception:      # noqa: BLE001
            pass

    # -- the launch calls these ----------------------------------------------------------
    def inflight(self, what: str) -> None:
        """Mark a create call as about to be sent (committed first — a crash mid-call is
        then known to have maybe-created it)."""
        if self.row is None:
            return
        self.row.inflight = what
        self.step(f"creating {what}")
        self.save()

    def assets(self, creative_id=None, spark_code_id=None) -> None:
        if self.row is None:
            return
        if creative_id:
            self.row.creative_id = int(creative_id)
        if spark_code_id:
            self.row.spark_code_id = int(spark_code_id)

    def campaign(self, campaign_id: str, reused: bool = False, name: str = "") -> None:
        if self.row is None:
            return
        self.row.campaign_id = str(campaign_id or "")
        if name:
            self.row.campaign_name = str(name)[:500]
        self.row.inflight = ""
        self.step(("continuing in campaign " if reused else "campaign created ") + str(campaign_id))
        self.save()

    def adgroup(self, i: int, adgroup_id: str) -> None:
        if self.row is None:
            return
        self.adgroups.append({"i": int(i), "id": str(adgroup_id), "ads": 0})
        self.row.inflight = ""
        self.step(f"ad group {i + 1} created {adgroup_id}")
        self.save()

    def skipped(self, i: int, adgroup_id: str = "", ads: int = 1) -> None:
        """Resume: an ad group the earlier attempt already finished."""
        if self.row is None:
            return
        self.adgroups.append({"i": int(i), "id": str(adgroup_id), "ads": int(ads), "kept": True})
        self.step(f"ad group {i + 1} already built — kept")

    def ad(self, i: int, ad_ids=None) -> None:
        if self.row is None:
            return
        for g in reversed(self.adgroups):
            if g["i"] == int(i):
                g["ads"] = int(g.get("ads") or 0) + 1
                if ad_ids:
                    g.setdefault("ad_ids", []).extend(str(x) for x in ad_ids)
                break
        self.row.inflight = ""
        self.step(f"ad created in ad group {i + 1}")
        self.save()

    def note(self, text: str) -> None:
        if self.row is None:
            return
        self.step(text)

    def finish(self, log) -> None:
        """Called just before the launch's final commit (so no commit here)."""
        if self.row is None:
            return
        try:
            ads = sum(int(g.get("ads") or 0) for g in self.adgroups)
            if log.ok:
                self.row.status = "ok"
            elif ads or (log.campaign_id and any(g.get("kept") for g in self.adgroups)):
                self.row.status = "partial"
            else:
                self.row.status = "failed"
            self.row.campaign_id = log.campaign_id or ""     # cleared when an empty shell was deleted
            self.row.error = (log.error_message or "")[:1000]
            self.step("done" if log.ok else f"stopped: {(log.error_code or '')} {(log.error_message or '')[:120]}")
            self.row.steps = json.dumps(bound_steps(self.steps))
            self.row.adgroups = json.dumps(self.adgroups)
            self.row.updated_at = _now()
        except Exception:      # noqa: BLE001
            pass

    def link(self, log) -> None:
        if self.row is not None and getattr(log, "id", None):
            try:
                self.row.log_id = log.id
                from .database import safe_commit
                safe_commit(self.db)
            except Exception:      # noqa: BLE001
                pass


# ---------------------------------------------------------------------------------------
# pure helpers (tested directly)

def ad_inflight(i: int) -> str:
    return f"ad (ad group {int(i) + 1})"


def ad_count(adgroups: list) -> int:
    return sum(int(g.get("ads") or 0) for g in adgroups or [])


def interrupted_message(campaign_id: str, adgroups: list, inflight: str, mark: str) -> tuple[str, str]:
    """(plain message, technical) for a launch a restart killed."""
    ads = ad_count(adgroups)
    groups = len(adgroups or [])
    if campaign_id and ads:
        msg = (f"The server restarted mid-launch. Campaign {campaign_id} exists with {groups} ad group(s) and "
               f"{ads} ad(s) — part of this launch went live. Use Retry failed to finish it inside the same campaign.")
    elif campaign_id:
        msg = (f"The server restarted mid-launch, after campaign {campaign_id} was created but before any ad. "
               "Use Retry failed to finish it inside the same campaign.")
    elif inflight:
        msg = (f"The server restarted while the {inflight} was being created, so it {mark} — check the account "
               "in Ads Manager before launching here again.")
    else:
        msg = "The server restarted before this account's launch reached TikTok — nothing was created. Safe to retry."
    tech = f"interrupted; campaign={campaign_id or '-'} adgroups={groups} ads={ads} inflight={inflight or '-'}"
    return msg, tech


def resume_plan(trace_row, logs_error: str = "") -> dict | None:
    """What a resumed launch keeps, or None when this trace can't be resumed safely.
    Not for Smart+ (TikTok builds its campaign, group and ad in one chain) and not when the
    campaign itself may exist unseen (no id to continue in)."""
    if trace_row is None or not (trace_row.campaign_id or "").strip():
        return None
    if getattr(trace_row, "smart_plus", False):
        return None
    groups = _load(trace_row.adgroups, [])
    inflight = (trace_row.inflight or "")
    done, empty = [], []
    for g in groups:
        if int(g.get("ads") or 0) > 0:
            done.append(int(g["i"]))
        elif inflight == ad_inflight(int(g["i"])):
            done.append(int(g["i"]))          # its ad may exist unseen — never add a second one
        elif g.get("id"):
            empty.append(str(g["id"]))
    return {"campaign_id": trace_row.campaign_id, "campaign_name": getattr(trace_row, "campaign_name", "") or "",
            "done": sorted(set(done)), "empty_adgroups": empty,
            "creative_id": trace_row.creative_id, "spark_code_id": trace_row.spark_code_id}


def recover(db, models, mark: str, stale: bool = False) -> int:
    """Traces still 'running' belong to launches a restart killed (boot) — or, with
    stale=True, to ones silent for STALE_AFTER. Each becomes a failed launch-log row that
    says what exists. Returns how many were closed."""
    q = db.query(models.LaunchTrace).filter(models.LaunchTrace.status == "running")
    if stale:
        q = q.filter(models.LaunchTrace.updated_at < _now() - STALE_AFTER)
    n = 0
    for t in q.limit(500).all():
        groups = _load(t.adgroups, [])
        msg, tech = interrupted_message(t.campaign_id or "", groups, t.inflight or "", mark)
        t.status = "interrupted"
        steps = _load(t.steps, [])
        steps.append({"t": _now().strftime("%H:%M:%S"), "s": "interrupted by a restart"})
        t.steps = json.dumps(bound_steps(steps))
        t.error = msg
        if not t.log_id:
            acct = db.query(models.AdAccount).filter_by(advertiser_id=t.advertiser_id).first()
            log = models.LaunchLog(batch_ref=t.batch_ref, advertiser_id=t.advertiser_id,
                                   advertiser_name=(acct.advertiser_name if acct else "") or "",
                                   campaign_id=t.campaign_id or "", ok=False, error_code="INTERRUPTED",
                                   error_message=msg, error_technical=tech,
                                   spark_code_id=t.spark_code_id)
            db.add(log)
            db.flush()
            t.log_id = log.id
        n += 1
    if n:
        db.commit()
    return n


def for_batch(db, models, batch_ref: str) -> dict:
    """{advertiser_id: latest trace row} for a batch."""
    out = {}
    for t in (db.query(models.LaunchTrace).filter_by(batch_ref=batch_ref)
              .order_by(models.LaunchTrace.id).all()):
        out[t.advertiser_id] = t
    return out


def view(t) -> dict:
    return {"status": t.status, "campaign_id": t.campaign_id or "", "steps": _load(t.steps, []),
            "adgroups": len(_load(t.adgroups, [])), "ads": ad_count(_load(t.adgroups, [])),
            "resumed": bool(getattr(t, "resumed", False))}


def prune(db, models, days: int = 30) -> int:
    cut = _now() - timedelta(days=days)
    n = db.query(models.LaunchTrace).filter(models.LaunchTrace.created_at < cut,
                                            models.LaunchTrace.status != "running").delete(synchronize_session=False)
    return int(n or 0)
