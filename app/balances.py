"""BC wallet + ad account balance sync, and the low-balance alert engine.

Alert policy (as specified): when a BC wallet drops below its threshold
($50 default), raise ONE in-app alert at the crossing, then remind at most
once every 24h while it stays low. Acknowledged alerts disappear from the
bell; the Overview banner shows any unacknowledged warn/err alerts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import config, models, queries, tiktok_api

REMIND_EVERY = timedelta(hours=24)

# Business Centers whose wallet TikTok refuses to show us. Being a Business Center Admin
# is not the same as having a finance role there, and on a BC without one every sweep
# spends two calls to be told "You don't have finance permission" — twice per BC, every
# time, forever. So the refusal is remembered and that BC is skipped, and re-tried after
# RETRY_FINANCE so a role granted later starts working on its own.
FINANCE_BLOCK_KEY = "finance_blocked_bcs"
RETRY_FINANCE = timedelta(days=7)


def _finance_blocked(db: Session) -> dict:
    import json as _json
    try:
        return _json.loads(queries.get_setting(db, FINANCE_BLOCK_KEY, "") or "{}")
    except ValueError:
        return {}


def finance_skip(db: Session) -> set[str]:
    """BC ids to skip this sweep — refused recently enough that asking again is waste."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = set()
    for bc_id, at in _finance_blocked(db).items():
        try:
            when = datetime.fromisoformat(at)
        except (TypeError, ValueError):
            continue
        if now - when < RETRY_FINANCE:
            out.add(str(bc_id))
    return out


def note_finance_refusal(db: Session, bc_id: str, message: str) -> bool:
    """True when this error was a finance-permission refusal (and is now remembered)."""
    if "finance permission" not in (message or "").lower():
        return False
    import json as _json
    blocked = _finance_blocked(db)
    blocked[str(bc_id)] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    queries.set_setting(db, FINANCE_BLOCK_KEY, _json.dumps(blocked))
    return True



def bc_portal_url(bc_id: str) -> str:
    """Deep link to this BC in TikTok's Business Center portal (verified
    format: business.tiktok.com/manage/overview?org_id=<bc_id>)."""
    return f"https://business.tiktok.com/manage/overview?org_id={bc_id}"


def bc_threshold(bc: models.BusinessCenter) -> float:
    """Per-BC override, falling back to the BC_LOW_BALANCE_THRESHOLD env ($50)."""
    return float(bc.alert_threshold or config.BC_LOW_BALANCE_THRESHOLD or 50.0)


def resync_structure(db: Session) -> dict:
    """Re-pull the BC list + account↔BC mapping with the stored token — the
    same sync 'Connect' runs. Heals: BCs synced before multi-BC support,
    accounts whose access was lost/regained, and BC ownership changes."""
    from .routes.oauth import sync_accounts  # local import — no cycle at load time
    total = {"count": 0, "bc_count": 0}
    for token, acct in queries.distinct_tokens(db):      # every connected TikTok login (v116)
        r = sync_accounts(db, token, acct.refresh_token or "", acct.token_expires_at, acct.refresh_expires_at,
                          user_id=acct.owner_user_id)
        total["count"] += int(r.get("count") or 0); total["bc_count"] += int(r.get("bc_count") or 0)
    return total


def sync_bc_balances(db: Session) -> int:
    """Refresh every BC wallet balance. Returns how many BCs were updated."""
    if not queries.any_access_token(db):
        return 0
    updated = 0
    now = datetime.now(timezone.utc)
    skip = finance_skip(db)
    for bc in db.query(models.BusinessCenter).all():
        if bc.bc_id in skip or bc.retired:
            continue
        token = queries.token_for_bc(db, bc.bc_id)          # the login that can see this BC
        try:
            bal, cur = tiktok_api.parse_bc_balance(
                tiktok_api.get_bc_balance(token, bc.bc_id))
            bc.balance = bal
            if cur:
                bc.currency = cur
            bc.last_synced_at = now
            updated += 1
        except tiktok_api.TikTokError as e:
            note_finance_refusal(db, bc.bc_id, e.message)
        # commit per BC — SQLite has one writer, and holding it open across the NEXT
        # BC's network call is what locks every page out. Release it each round.
        db.commit()
    return updated


def _parse_account_balance(item: dict) -> float | None:
    """TikTok returns account balances under varying keys/shapes — try them all."""
    for key in ("balance", "cash_balance", "available_balance", "valid_balance",
                "advertiser_balance", "total_balance"):
        v = item.get(key)
        if isinstance(v, dict):   # e.g. {"amount": "12.34", "currency": "USD"}
            v = v.get("amount", v.get("balance"))
        if v is None or v == "":
            continue
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def sync_account_balances(db: Session) -> int:
    """Refresh ad-account balances per BC via /advertiser/balance/get/."""
    import json as _json
    import time as _time
    if not queries.any_access_token(db):
        return 0
    accounts = {a.advertiser_id: a for a in db.query(models.AdAccount).all()}
    updated = 0
    errors: list[str] = []
    skip = finance_skip(db)
    for bc in db.query(models.BusinessCenter).all():
        if bc.bc_id in skip or bc.retired:
            continue
        token = queries.token_for_bc(db, bc.bc_id)          # the login that can see this BC
        page = 1
        while True:
            try:
                data = tiktok_api.get_advertiser_balances(token, bc.bc_id, page=page)
            except tiktok_api.TikTokError as e:
                if note_finance_refusal(db, bc.bc_id, e.message):
                    errors.append(f"BC {bc.bc_id}: no finance role here — skipping it for a week")
                else:
                    errors.append(f"BC {bc.bc_id} balances failed (code {e.code}: {str(e.message)[:60]})")
                break
            items = (data.get("list") or data.get("balance_list")
                     or data.get("advertiser_balances") or [])
            for item in items:
                acct = accounts.get(str(item.get("advertiser_id", item.get("adv_id", ""))))
                if acct is None:
                    continue
                bal = _parse_account_balance(item)
                if bal is not None:
                    acct.balance = bal
                    updated += 1
            db.commit()   # commit this page before fetching the next — never hold the
            #               one writer open across the next network call
            total_pages = int((data.get("page_info", {}) or {}).get("total_page", 1) or 1)
            if page >= total_pages:
                break
            page += 1
        _time.sleep(0.15)
    queries.set_setting(db, "balance_report", _json.dumps({
        "updated": updated, "errors": errors[:10]}))
    db.commit()
    return updated


# ---------------------------------------------------------------------------
# shared wallets + runway (pure — tested directly)
# ---------------------------------------------------------------------------

def shared_wallet(wallet, balances: list) -> float | None:
    """The one balance every account reports when the BC runs a SHARED wallet, else None.
    Under a shared wallet TikTok answers each account's balance with the wallet's own, so
    adding them up counts the same money once per account ($25 read as $1,905). Seen as:
    at least two accounts, 80%+ of them on the same positive figure, and that figure is the
    wallet's (or the wallet itself wasn't readable)."""
    vals = [round(float(b), 2) for b in balances if b is not None]
    if len(vals) < 2:
        return None
    counts: dict[float, int] = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    v, n = max(counts.items(), key=lambda kv: kv[1])
    if v <= 0 or n < 2 or n < 0.8 * len(vals):
        return None
    if wallet is None or float(wallet or 0) == 0 or abs(float(wallet) - v) < 0.01:
        return v
    return None


def money_in_bc(wallet, balances: list) -> dict:
    """{wallet, in_accounts, total, shared} — the shared case counts the money once."""
    w = float(wallet or 0)
    sv = shared_wallet(wallet, balances)
    if sv is not None:
        return {"wallet": max(w, sv), "in_accounts": 0.0, "total": max(w, sv), "shared": True}
    ia = sum(float(b or 0) for b in balances)
    return {"wallet": w, "in_accounts": ia, "total": w + ia, "shared": False}


def daily_burn(last7: float, days_with_data: int, spend_today: float, day_fraction: float) -> float:
    """Average spend per day: the last 7 full days when there's history, else today's
    spend projected over the whole day (never today's partial spend as if it were a day —
    at 9 am that made a week's money look like a month)."""
    if days_with_data > 0 and last7 > 0:
        return last7 / max(days_with_data, 1)
    if spend_today > 0:
        return spend_today / max(min(day_fraction, 1.0), 0.1)
    return 0.0


def runway_days(total: float, burn: float) -> float | None:
    return (float(total) / burn) if burn > 0 else None


def burn_by_account(db: Session, advertiser_ids: list[str]) -> tuple[dict, dict]:
    """({advertiser_id: spend over the last 7 full local days}, {advertiser_id: days with data})."""
    from sqlalchemy import func
    from . import timeutil
    start = timeutil.local_date_str(timeutil.local_midnight_utc(-7))
    end = timeutil.local_date_str(timeutil.local_midnight_utc(0))
    spend: dict = {}
    days: dict = {}
    ids = [a for a in advertiser_ids if a]
    for i in range(0, len(ids), 500):
        for aid, day, sp in (db.query(models.SpendSnapshot.advertiser_id, models.SpendSnapshot.day,
                                      func.sum(models.SpendSnapshot.spend))
                             .filter(models.SpendSnapshot.advertiser_id.in_(ids[i:i + 500]),
                                     models.SpendSnapshot.day >= start, models.SpendSnapshot.day < end)
                             .group_by(models.SpendSnapshot.advertiser_id, models.SpendSnapshot.day)):
            spend[aid] = spend.get(aid, 0.0) + float(sp or 0)
            days.setdefault(aid, set()).add(day)
    return spend, {k: len(v) for k, v in days.items()}


def _latest_alert(db: Session, kind: str, ref_id: str) -> models.Alert | None:
    return (db.query(models.Alert).filter_by(kind=kind, ref_id=ref_id)
            .order_by(models.Alert.created_at.desc()).first())


def evaluate_bc_alerts(db: Session) -> list[models.Alert]:
    """Create low-balance alerts per the crossing/reminder policy."""
    created: list[models.Alert] = []
    now = datetime.now(timezone.utc)
    for bc in db.query(models.BusinessCenter).all():
        if bc.retired:
            continue                                   # v155.27: a removed BC never nags about its wallet
        threshold = bc_threshold(bc)
        label = bc.name or bc.bc_id
        if bc.balance is not None and bc.balance < threshold:
            last = _latest_alert(db, "bc_low_balance", bc.bc_id)
            recent = False
            if last:
                last_at = last.created_at
                if last_at is not None and last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
                recent = last_at is not None and (now - last_at) < REMIND_EVERY
            if not recent:
                alert = models.Alert(
                    kind="bc_low_balance", ref_id=bc.bc_id, level="warn",
                    message=(f"Business Center “{label}” wallet is low: "
                             f"{bc.currency} {bc.balance:.2f} (threshold {threshold:.0f}). "
                             "Top it up to keep campaigns funded."))
                db.add(alert)
                created.append(alert)
    db.commit()
    return created


def unacknowledged(db: Session, limit: int = 50) -> list[models.Alert]:
    return (db.query(models.Alert).filter_by(acknowledged=False)
            .order_by(models.Alert.created_at.desc()).limit(limit).all())


def run_sweep(db: Session) -> dict:
    """One full pass: balances + alerts. Called by the background worker
    and by the manual 'Sync now' actions."""
    bcs = sync_bc_balances(db)
    accts = sync_account_balances(db)
    alerts = evaluate_bc_alerts(db)
    return {"bcs": bcs, "accounts": accts, "new_alerts": len(alerts)}
