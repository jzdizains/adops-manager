"""Monitor — the per-BC operations dashboard.

For every Business Center: wallet balance (red under threshold), and for every
ad account under it: account status, balance, campaign status, last error, and
today's metrics (Spend / CPM / CPC / CPA / CTR / launch date).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import balances, models, queries
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/monitor")
def monitor(request: Request, db: Session = Depends(get_db)):
    bcs = db.query(models.BusinessCenter).order_by(models.BusinessCenter.name).all()
    accounts = db.query(models.AdAccount).order_by(models.AdAccount.advertiser_name).all()
    campaigns = db.query(models.CampaignRecord).all()

    camps_by_acct: dict[str, list[models.CampaignRecord]] = {}
    for c in campaigns:
        camps_by_acct.setdefault(c.advertiser_id, []).append(c)

    # last error per account (most recent failed launch)
    last_errors: dict[str, models.LaunchLog] = {}
    for log in (db.query(models.LaunchLog).filter_by(ok=False)
                .order_by(models.LaunchLog.created_at.desc()).limit(300)):
        last_errors.setdefault(log.advertiser_id, log)

    def account_row(a: models.AdAccount) -> dict:
        camps = sorted(camps_by_acct.get(a.advertiser_id, []),
                       key=lambda c: c.spend_today, reverse=True)
        active = [c for c in camps if c.operation_status == "ENABLE"]
        return {
            "a": a, "campaigns": camps, "active_count": len(active),
            "spend": sum(c.spend_today for c in camps),
            "err": last_errors.get(a.advertiser_id),
        }

    groups = []
    seen = set()
    for bc in bcs:
        rows = [account_row(a) for a in accounts if a.owner_bc_id == bc.bc_id]
        seen.update(r["a"].advertiser_id for r in rows)
        groups.append({
            "bc": bc, "rows": rows,
            "low": bc.balance is not None and bc.balance < balances.bc_threshold(bc),
            "spend": sum(r["spend"] for r in rows),
        })
    orphans = [account_row(a) for a in accounts if a.advertiser_id not in seen]

    # account inventory: fresh (never launched), active, cooled-down
    from .. import rules as rules_mod
    from .super_launcher import eligible_accounts
    fresh = len(eligible_accounts(db, "new_only", 10_000))
    cooling = sum(1 for a in accounts if rules_mod.in_cooldown(a))
    with_active = len({c.advertiser_id for c in campaigns if c.operation_status == "ENABLE"})

    return render(request, "monitor.html", {
        "title": "Monitor", "groups": groups, "orphans": orphans,
        "synced_ago": queries.campaigns_synced_ago(db),
        "inventory": {"fresh": fresh, "active": with_active, "cooling": cooling},
    })
