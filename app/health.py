"""One health state per campaign, derived from what the sweep already reads.

TikTok reports a campaign's real state on its AD GROUPS (secondary_status): in review, out of
money, budget spent, rejected, scheduled… The Campaigns page used to look only at the
campaign-level status, so a campaign whose every ad group was stuck in review — or out of
money — sat under "Active". This derives one state per campaign from the account status, the
campaign's own switch and its ad groups' statuses (kept by adgroup_stats → AdgroupState),
with no extra TikTok calls.

Precedence (first match wins) — ported from the reference tool, whose mapping was checked
against live accounts:
  1. account not enabled / ad group says the account is punished   → ACCOUNT_SUSPENDED
  3. campaign switched off (its own switch, or every group off)     → PAUSED
  4. any ad group delivering                                        → ACTIVE (mix shown)
  5. AUDIT_DENY                                                     → REJECTED
  6. BALANCE_EXCEED                                                 → NO_FUNDS
  7. BUDGET_EXCEED (its own budget) / CAMPAIGN_EXCEED (campaign
     budget — but with NO campaign budget TikTok means "out of money") → BUDGET_CAPPED / NO_FUNDS
  8. AUDIT / REAUDIT                                                → IN_REVIEW
  9. NOT_START / CREATE                                             → SCHEDULED
 10. TIME_DONE                                                      → ENDED
 else UNKNOWN (TikTok's raw status shown, never a guess)
Pure: tested directly.
"""
from __future__ import annotations

ACTIVE, PAUSED, IN_REVIEW, SCHEDULED = "ACTIVE", "PAUSED", "IN_REVIEW", "SCHEDULED"
REJECTED, NO_FUNDS, BUDGET_CAPPED, ENDED = "REJECTED", "NO_FUNDS", "BUDGET_CAPPED", "ENDED"
ACCOUNT_SUSPENDED, LAUNCH_FAILED, UNKNOWN = "ACCOUNT_SUSPENDED", "LAUNCH_FAILED", "UNKNOWN"

# which Campaigns-page tab each state belongs to (the page keeps its Active / Pending / Blocked / Paused tabs)
BUCKET = {
    ACTIVE: "active", BUDGET_CAPPED: "active", ENDED: "active", UNKNOWN: "active",
    IN_REVIEW: "pending", SCHEDULED: "pending",
    REJECTED: "blocked", ACCOUNT_SUSPENDED: "blocked", LAUNCH_FAILED: "blocked", NO_FUNDS: "blocked",
    PAUSED: "paused",
}
# the row chip's colour (our existing pill classes)
PILL = {ACTIVE: "ok", BUDGET_CAPPED: "warn", ENDED: "dim", UNKNOWN: "dim", IN_REVIEW: "warn", SCHEDULED: "dim",
        REJECTED: "err", ACCOUNT_SUSPENDED: "err", LAUNCH_FAILED: "err", NO_FUNDS: "err", PAUSED: "dim"}


# advertiser statuses under which nothing delivers (the same list the Campaigns page used)
ACCOUNT_BLOCK_TOKENS = ("PUNISH", "LIMIT", "DISABLE", "CONFIRM_FAIL", "PENDING")


def _up(s) -> str:
    return str(s or "").upper()


def mix(groups: list[dict]) -> dict:
    """How the ad groups spread over states — '16 delivering · 4 in review' on a scaled campaign."""
    m = {"total": len(groups), "delivering": 0, "in_review": 0, "scheduled": 0, "rejected": 0,
         "paused": 0, "capped": 0, "no_funds": 0, "other": 0}
    for g in groups:
        s, off = _up(g.get("secondary_status")), _up(g.get("operation_status")) == "DISABLE"
        if off or "CAMPAIGN_DISABLE" in s or "ADGROUP_STATUS_DISABLE" in s:
            m["paused"] += 1
        elif "AUDIT_DENY" in s:
            m["rejected"] += 1
        elif "DELIVERY_OK" in s:
            m["delivering"] += 1
        elif "BALANCE_EXCEED" in s:
            m["no_funds"] += 1
        elif s in ("ADGROUP_STATUS_AUDIT", "ADGROUP_STATUS_REAUDIT") or s.endswith("_STATUS_CREATE"):
            m["in_review"] += 1
        elif "NOT_START" in s:
            m["scheduled"] += 1
        elif "BUDGET_EXCEED" in s or "CAMPAIGN_EXCEED" in s:
            m["capped"] += 1
        else:
            m["other"] += 1
    return m


def mix_label(m: dict) -> str:
    names = (("delivering", "delivering"), ("in_review", "in review"), ("scheduled", "scheduled"),
             ("rejected", "rejected"), ("paused", "paused"), ("capped", "budget-capped"),
             ("no_funds", "no funds"), ("other", "other"))
    return " · ".join(f"{m[k]} {label}" for k, label in names if m.get(k))


def derive(account_status: str, groups: list[dict], campaign_operation_status: str = "",
           campaign_budget_mode: str = "", campaign_budget: float = 0.0) -> tuple[str, str]:
    """(state, detail) for one campaign. `groups` = [{operation_status, secondary_status}]."""
    ast = _up(account_status)
    if ast and ast != "STATUS_ENABLE" and any(t in ast for t in ACCOUNT_BLOCK_TOKENS):
        return ACCOUNT_SUSPENDED, account_status
    secs = [_up(g.get("secondary_status")) for g in groups]

    def find(pred):
        for g, s in zip(groups, secs):
            if pred(s):
                return g.get("secondary_status") or ""
        return ""

    if any("ADVERTISER_ACCOUNT_PUNISH" in s for s in secs):
        return ACCOUNT_SUSPENDED, find(lambda s: "ADVERTISER_ACCOUNT_PUNISH" in s)
    if _up(campaign_operation_status) == "DISABLE":
        return PAUSED, ""
    if groups and all(_up(g.get("operation_status")) == "DISABLE" for g in groups):
        return PAUSED, ""
    if groups and all("CAMPAIGN_DISABLE" in s for s in secs):
        return PAUSED, "campaign switched off"
    m = mix(groups)
    if m["delivering"]:
        return ACTIVE, (mix_label(m) if m["delivering"] < m["total"] else "")
    if any("AUDIT_DENY" in s for s in secs):
        return REJECTED, find(lambda s: "AUDIT_DENY" in s)
    if any("BALANCE_EXCEED" in s for s in secs):
        return NO_FUNDS, find(lambda s: "BALANCE_EXCEED" in s)
    if any("BUDGET_EXCEED" in s for s in secs):
        return BUDGET_CAPPED, find(lambda s: "BUDGET_EXCEED" in s)
    if any("CAMPAIGN_EXCEED" in s for s in secs):
        has_budget = _up(campaign_budget_mode) in ("BUDGET_MODE_DAY", "BUDGET_MODE_TOTAL") and float(campaign_budget or 0) > 0
        return (BUDGET_CAPPED if has_budget else NO_FUNDS), find(lambda s: "CAMPAIGN_EXCEED" in s)
    if any(s in ("ADGROUP_STATUS_AUDIT", "ADGROUP_STATUS_REAUDIT") for s in secs):
        return IN_REVIEW, find(lambda s: s in ("ADGROUP_STATUS_AUDIT", "ADGROUP_STATUS_REAUDIT"))
    # (an ad group still in CREATE is normally seconds from review — and the launcher already
    # deletes any campaign it couldn't put an ad in — so it reads as pending, never "failed")
    if any("NOT_START" in s or s.endswith("_STATUS_CREATE") for s in secs):
        return SCHEDULED, find(lambda s: "NOT_START" in s or s.endswith("_STATUS_CREATE"))
    if any("TIME_DONE" in s for s in secs):
        return ENDED, find(lambda s: "TIME_DONE" in s)
    if any("DELIVERY_OK" in s for s in secs):
        return ACTIVE, ""
    return UNKNOWN, (groups[0].get("secondary_status") or "") if groups else ""


EXPLAIN = {
    ACCOUNT_SUSPENDED: ("Account can't deliver", "The ad account is suspended, limited or still pending review — nothing on it delivers.",
                        "Check the account in Business Center (appeal a suspension); campaigns resume by themselves once it's cleared."),
    LAUNCH_FAILED: ("Launch never produced an ad", "The ad group exists but has no ads, so there's nothing to serve.",
                    "Open the launch result for the error, fix it and relaunch."),
    REJECTED: ("Rejected", "TikTok disapproved the ads — the campaign is on but nothing delivers.",
               "Appeal from the drawer, or swap the creative and relaunch."),
    NO_FUNDS: ("Out of money", "The ad account's balance ran out. (TikTok words an empty account as a budget cap.)",
               "Top up the account — delivery resumes by itself."),
    BUDGET_CAPPED: ("Budget spent", "It spent today's budget, so TikTok stopped it until tomorrow.",
                    "Raise the daily budget to keep spending today, or leave it — normal pacing."),
    IN_REVIEW: ("In review", "TikTok hasn't decided on the ads yet.",
                "Usually under an hour. Several hours means it's stuck — check Ads Manager."),
    SCHEDULED: ("Scheduled", "Ready, waiting for its start time.", "Nothing to do."),
    ENDED: ("Ended", "It reached its end time and stopped.", "Extend the end date to keep it running."),
    PAUSED: ("Paused", "Switched off (here or in Ads Manager), not by TikTok.", "Use the toggle on the row."),
    ACTIVE: ("Delivering", "At least one ad group is serving.", "Nothing to do."),
    UNKNOWN: ("Unrecognised status", "TikTok reported a status we don't map yet — its raw value is shown.",
              "Check it in Ads Manager."),
}


def explain(state: str, detail: str = "") -> dict:
    title, meaning, fix = EXPLAIN.get(state, EXPLAIN[UNKNOWN])
    return {"state": state, "title": title, "meaning": meaning, "fix": fix, "raw": detail or "",
            "bucket": BUCKET.get(state, "active"), "pill": PILL.get(state, "dim")}


# statuses that mean "still waiting on TikTok" — leaving them for delivery / rejection is the
# moment worth telling the operator about
PENDING_TOKENS = ("ADGROUP_STATUS_AUDIT", "ADGROUP_STATUS_REAUDIT", "_STATUS_CREATE", "NOT_START")


def is_pending(status: str) -> bool:
    s = _up(status)
    return s in ("ADGROUP_STATUS_AUDIT", "ADGROUP_STATUS_REAUDIT") or s.endswith("_STATUS_CREATE") or "NOT_START" in s


def transition(old: str, new: str) -> str:
    """'delivering' / 'rejected' when an ad group LEAVES review that way, else ''."""
    if not is_pending(old):
        return ""
    n = _up(new)
    if "DELIVERY_OK" in n:
        return "delivering"
    if "AUDIT_DENY" in n:
        return "rejected"
    return ""
