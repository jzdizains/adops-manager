"""Assistant — Claude with read-only tools over this dashboard's data.

The model can READ everything (P&L, campaigns, accounts, creatives, inbox,
hourly delivery) and can only PROPOSE changes: `propose_actions` returns the
list to the page, which shows an approval card; the operator ticks what to
apply and the page calls the same JSON endpoints the Campaigns console uses.
Nothing here writes to TikTok.

Conversation storage: every message is kept as the API content blocks
(text / tool_use / tool_result) so a chat resumes with its history intact.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import config, creative_perf, hourly, inbox as inbox_mod, models, pnl_data, timeutil

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_ROUNDS = 8                 # tool-call rounds per user turn
MAX_TOKENS = 2000
HISTORY_LIMIT = 40             # messages sent back to the model per turn

RANGES = ("today", "yesterday", "7d", "30d", "mtd")

SYSTEM = """You are the analyst inside a TikTok ads operations dashboard used by a media buyer who runs hundreds of ad accounts across several Business Centers, uploads creatives and spark codes, and launches campaigns with presets.

Facts about the data:
- Spend comes from TikTok (per campaign, per local day). Revenue comes from Glitchy postbacks, joined to campaigns by a `source` value that rides the landing URL. Profit = revenue − spend. ROAS = revenue ÷ spend. EPC = revenue ÷ TikTok clicks.
- A source shared by several campaigns has its revenue split by spend share. Campaigns launched without a source have spend but can never show revenue.
- Account states: fresh (never launched), live (active campaign), used, cooling (after failed launches), blocked (TikTok status / punished BC).
- Business Center wallets fund ad accounts; low wallets stop delivery.

How to work:
- Always pull numbers with the tools before stating them; never invent figures. Say which range you used.
- Be concise and concrete: lead with the number that matters (profit, ROAS), then the why. Use short markdown tables for comparisons (max ~15 rows), plain sentences otherwise. No emoji.
- You cannot change anything yourself. When the operator asks to pause/resume campaigns or change budgets or cost caps, call `propose_actions` with the exact campaigns and values — the dashboard shows an approval card and the operator applies it. Say clearly that nothing changes until they approve. Never claim an action was applied.
- If the API key or a tool fails, say so plainly."""

TOOLS = [
    {"name": "overview", "description": "Totals for a range: spend, revenue, profit, ROAS, conversions, clicks, EPC — plus the same for the prior period of equal length.",
     "input_schema": {"type": "object", "properties": {"range": {"type": "string", "enum": list(RANGES), "description": "today (default), yesterday, 7d, 30d, mtd"}}, "required": []}},
    {"name": "pnl", "description": "Profit breakdown for a range by source, Business Center (bc), account, creative or spark. Rows sorted by profit, best first.",
     "input_schema": {"type": "object", "properties": {"range": {"type": "string", "enum": list(RANGES)}, "by": {"type": "string", "enum": ["source", "bc", "account", "creative", "spark"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 60}}, "required": ["by"]}},
    {"name": "campaigns", "description": "Today's campaigns with spend, revenue, profit, ROAS, conversions, status, account and Business Center. Filter by state (active|paused|blocked|all), a name/account/source search, minimum spend; sort by profit|spend|roas|epc.",
     "input_schema": {"type": "object", "properties": {"state": {"type": "string", "enum": ["active", "paused", "blocked", "all"]}, "q": {"type": "string"}, "min_spend": {"type": "number"}, "sort": {"type": "string", "enum": ["profit", "spend", "roas", "epc", "worst"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": []}},
    {"name": "accounts", "description": "Every ad account with Business Center, state (fresh/live/used/cooling/blocked), balance, spend today, profit today, campaigns, plus BC wallet balances.",
     "input_schema": {"type": "object", "properties": {"state": {"type": "string", "enum": ["fresh", "active", "used", "cooldown", "blocked", "all"]}, "bc": {"type": "string", "description": "Business Center name filter"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": []}},
    {"name": "creatives", "description": "Creative performance for a range, rolled up by original upload (family): spend, revenue, profit, ROAS, variants, how many accounts it ran in, and the fresh (never launched) inventory count.",
     "input_schema": {"type": "object", "properties": {"range": {"type": "string", "enum": list(RANGES)}, "limit": {"type": "integer", "minimum": 1, "maximum": 60}}, "required": []}},
    {"name": "inbox", "description": "Everything needing attention right now: blocked accounts, rejected ads, failed launches, low wallets, rule actions.",
     "input_schema": {"type": "object", "properties": {"level": {"type": "string", "enum": ["err", "warn", "info", "all"]}}, "required": []}},
    {"name": "hourly", "description": "Hour-by-hour spend, clicks, conversions and revenue for one local day (today by default) across all campaigns.",
     "input_schema": {"type": "object", "properties": {"day": {"type": "string", "description": "YYYY-MM-DD local; default today"}}, "required": []}},
    {"name": "propose_actions", "description": "Propose changes for the operator to approve: pause/resume campaigns, set the daily budget of every ad group in a campaign, or set a cost cap. The dashboard shows an approval card; nothing happens until the operator applies it.",
     "input_schema": {"type": "object", "properties": {"title": {"type": "string", "description": "Short heading, e.g. 'Pause 3 losing campaigns'"}, "actions": {"type": "array", "items": {"type": "object", "properties": {
         "type": {"type": "string", "enum": ["pause", "resume", "budget", "cap"]}, "advertiser_id": {"type": "string"}, "campaign_id": {"type": "string"},
         "value": {"type": "number", "description": "budget or cap amount in $, ignored for pause/resume"}, "why": {"type": "string"}}, "required": ["type", "advertiser_id", "campaign_id"]}}}, "required": ["title", "actions"]}},
]


# ---------------------------------------------------------------- tool executors ----------------

def _range(r: str | None):
    r = r if r in RANGES else "today"
    s, e = timeutil.range_bounds(r)
    return r, s, e


def _money(v) -> float:
    return round(float(v or 0), 2)


def t_overview(db: Session, a: dict) -> dict:
    r, s, e = _range(a.get("range"))
    cur = pnl_data.overall_totals(db, s, e)
    prior = pnl_data.overall_totals(db, s - (e - s), s)
    def pack(t):
        return {"spend": _money(t["spend"]), "revenue": _money(t["revenue"]), "profit": _money(t["profit"]), "roas": round(t["roas"], 2),
                "conversions": t["conversions"], "clicks": t["clicks"], "epc": round(t["revenue"] / t["clicks"], 2) if t["clicks"] else 0.0}
    return {"range": r, "current": pack(cur), "prior_period": pack(prior), "local_time": timeutil.now_local().strftime("%Y-%m-%d %H:%M")}


def t_pnl(db: Session, a: dict) -> dict:
    from .routes.pnl_page import _slices
    r, s, e = _range(a.get("range"))
    by = a.get("by") if a.get("by") in ("source", "bc", "account", "creative", "spark") else "source"
    rows = _slices(db, s, e)[by][: int(a.get("limit") or 30)]
    return {"range": r, "by": by, "rows": [{"name": x["name"], "detail": x["sub"], "spend": _money(x["spend"]), "revenue": _money(x["revenue"]), "profit": _money(x["profit"]),
                                            "roas": round(x["roas"], 2), "conversions": int(x["conversions"] or 0)} for x in rows]}


def t_campaigns(db: Session, a: dict) -> dict:
    from .routes.status import blocked_reason
    state = a.get("state") or "active"
    q = (a.get("q") or "").strip().lower()
    min_spend = float(a.get("min_spend") or 0)
    sort = a.get("sort") or "profit"
    accounts = {x.advertiser_id: x for x in db.query(models.AdAccount).all()}
    bcs = {b.bc_id: b.name for b in db.query(models.BusinessCenter).all()}
    src_map = pnl_data.campaign_source_map(db)
    pb = pnl_data.revenue_by_source(db, timeutil.local_midnight_utc(0), timeutil.local_midnight_utc(1))
    recs = db.query(models.CampaignRecord).all()
    spend_by_src: dict[str, float] = {}
    for c in recs:
        src = src_map.get(c.campaign_id, "")
        if src:
            spend_by_src[src] = spend_by_src.get(src, 0.0) + float(c.spend_today or 0)
    out = []
    for c in recs:
        acct = accounts.get(c.advertiser_id)
        blocked = blocked_reason(c, acct) if c.operation_status == "ENABLE" else ""
        st = "blocked" if blocked else ("active" if c.operation_status == "ENABLE" else "paused")
        if state != "all" and st != state:
            continue
        src = src_map.get(c.campaign_id, "")
        sp = float(c.spend_today or 0)
        rev = 0.0
        if src and src in pb:
            tot = spend_by_src.get(src, 0.0)
            n_share = sum(1 for x in src_map.values() if x == src)
            share = (sp / tot) if tot else (1.0 / n_share if n_share else 0)
            rev = float(pb[src].get("revenue", 0.0)) * share
        if sp < min_spend:
            continue
        acct_name = (acct.advertiser_name if acct else "") or c.advertiser_id
        bc = bcs.get(acct.owner_bc_id, "") if acct else ""
        hay = f"{c.campaign_name} {acct_name} {bc} {src}".lower()
        if q and q not in hay:
            continue
        out.append({"campaign_id": c.campaign_id, "advertiser_id": c.advertiser_id, "name": c.campaign_name, "account": acct_name, "bc": bc, "state": st, "blocked": blocked,
                    "source": src, "spend": _money(sp), "revenue": _money(rev), "profit": _money(rev - sp), "roas": round(rev / sp, 2) if sp else 0.0,
                    "epc": round(rev / c.clicks, 2) if (c.clicks and rev) else 0.0, "conversions": int(c.conversions or 0), "clicks": int(c.clicks or 0),
                    "budget": float(c.budget or 0), "cpa": round(float(c.cpa or 0), 2), "launched": c.launched_at.strftime("%Y-%m-%d %H:%M") if c.launched_at else ""})
    keyf = {"profit": lambda x: -x["profit"], "spend": lambda x: -x["spend"], "roas": lambda x: -x["roas"], "epc": lambda x: -x["epc"], "worst": lambda x: x["profit"]}.get(sort, lambda x: -x["profit"])
    out.sort(key=keyf)
    lim = int(a.get("limit") or 40)
    return {"date": timeutil.local_date_str(), "state": state, "total_matching": len(out), "rows": out[:lim]}


def t_accounts(db: Session, a: dict) -> dict:
    from .routes import super_launcher as sl
    from .routes.dashboard import _account_facts
    accounts = db.query(models.AdAccount).filter(models.AdAccount.status != "ACCESS_LOST").order_by(models.AdAccount.advertiser_name).all()
    ctx = sl.account_picker_context(db, accounts)
    facts = _account_facts(db, accounts, ctx)
    bcs = {b.bc_id: b for b in db.query(models.BusinessCenter).all()}
    state = a.get("state") or "all"
    bcq = (a.get("bc") or "").strip().lower()
    rows = []
    for x in accounts:
        f = facts[x.advertiser_id]
        b = bcs.get(x.owner_bc_id or "")
        if state != "all" and f["state"] != state:
            continue
        if bcq and bcq not in ((b.name if b else "") or "").lower():
            continue
        rows.append({"advertiser_id": x.advertiser_id, "name": x.advertiser_name or x.advertiser_id, "bc": (b.name if b else "") or "", "state": f["state"], "reason": f["reason"],
                     "enabled": bool(x.enabled), "balance": _money(x.balance) if x.balance is not None else None, "spend": _money(f["spend"]), "profit": _money(f["profit"]),
                     "roas": round(f["roas"], 2), "campaigns_live": f["active"], "campaigns": f["total"]})
    rows.sort(key=lambda r: -r["profit"])
    counts = {k: sum(1 for f in facts.values() if f["state"] == k) for k in ("fresh", "active", "used", "cooldown", "blocked")}
    return {"counts": counts, "business_centers": [{"name": b.name or b.bc_id, "wallet": _money(b.balance), "currency": b.currency or "USD", "status": b.status or "", "low": (b.balance or 0) < float(b.alert_threshold or 50)} for b in bcs.values()],
            "rows": rows[: int(a.get("limit") or 100)]}


def t_creatives(db: Session, a: dict) -> dict:
    r, s, e = _range(a.get("range"))
    fams = creative_perf.families(creative_perf.rows(db, s, e, today=(r == "today")))
    fresh = db.query(func.count(models.Creative.id)).filter(models.Creative.status == "available", models.Creative.archived == False).scalar() or 0  # noqa: E712
    rows = []
    for f in fams[: int(a.get("limit") or 30)]:
        accs = {x["c"].used_advertiser_id for x in f["rows"] if x["c"].used_advertiser_id}
        rows.append({"name": f["name"], "variants": f["n"], "accounts": len(accs), "spend": _money(f["spend"]), "revenue": _money(f["revenue"]), "profit": _money(f["profit"]), "roas": round(f["roas"], 2)})
    return {"range": r, "fresh_inventory": fresh, "tested": len(fams), "rows": rows}


def t_inbox(db: Session, a: dict) -> dict:
    items = inbox_mod.build(db)
    lvl = a.get("level") or "all"
    if lvl != "all":
        items = [i for i in items if i["level"] == lvl]
    return {"counts": inbox_mod.counts(inbox_mod.build(db)), "items": [{"level": i["level"], "kind": i["kind"], "title": i["title"], "message": i["message"][:220], "where": i["where"]} for i in items[:40]]}


def t_hourly(db: Session, a: dict) -> dict:
    day = a.get("day") or timeutil.local_date_str()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        day = timeutil.local_date_str()
    cids = [r[0] for r in db.query(models.CampaignRecord.campaign_id).all()]
    hs = hourly.series(db, cids, day)
    rev = hourly.revenue_series(db, None, day)
    now_h = timeutil.now_local().hour if day == timeutil.local_date_str() else 23
    rows = [{"hour": h, "spend": round(hs["spend"][h], 2), "clicks": int(hs["clicks"][h]), "conversions": int(hs["conversions"][h]), "revenue": round(rev[h], 2), "profit": round(rev[h] - hs["spend"][h], 2)} for h in range(0, now_h + 1)]
    return {"day": day, "hour_now": now_h, "rows": rows}


def t_propose(db: Session, a: dict) -> dict:
    """Validate against the campaign cache; the page renders the card."""
    recs = {(c.advertiser_id, c.campaign_id): c for c in db.query(models.CampaignRecord).all()}
    names = {x.advertiser_id: (x.advertiser_name or x.advertiser_id) for x in db.query(models.AdAccount).all()}
    ok, bad = [], []
    for act in a.get("actions") or []:
        key = (str(act.get("advertiser_id") or ""), str(act.get("campaign_id") or ""))
        c = recs.get(key)
        t = act.get("type")
        if not c or t not in ("pause", "resume", "budget", "cap"):
            bad.append({"campaign_id": key[1], "error": "unknown campaign" if not c else "unknown action"})
            continue
        val = act.get("value")
        if t in ("budget", "cap") and not (isinstance(val, (int, float)) and val > 0):
            bad.append({"campaign_id": key[1], "error": f"{t} needs a positive $ value"})
            continue
        ok.append({"type": t, "advertiser_id": key[0], "campaign_id": key[1], "name": c.campaign_name, "account": names.get(key[0], key[0]),
                   "status": c.operation_status, "spend": _money(c.spend_today), "value": float(val) if val is not None else None, "why": (act.get("why") or "")[:160]})
    return {"proposed": True, "title": (a.get("title") or "Proposed changes")[:80], "actions": ok, "rejected": bad,
            "note": "Shown to the operator as an approval card. Nothing is applied until they click Apply."}


EXEC = {"overview": t_overview, "pnl": t_pnl, "campaigns": t_campaigns, "accounts": t_accounts, "creatives": t_creatives,
        "inbox": t_inbox, "hourly": t_hourly, "propose_actions": t_propose}


def run_tool(db: Session, name: str, args: dict) -> tuple[str, dict | None]:
    """→ (json text for the model, card dict for the page or None)."""
    fn = EXEC.get(name)
    if not fn:
        return json.dumps({"error": f"unknown tool {name}"}), None
    try:
        res = fn(db, args or {})
    except Exception as e:  # noqa: BLE001 — the model should see the failure, not a 500
        return json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}), None
    card = {**res, "type": "actions"} if name == "propose_actions" and res.get("actions") else None
    return json.dumps(res, default=str), card


# ---------------------------------------------------------------- the loop ---------------------

class AssistantError(Exception):
    pass


def _api(messages: list, model: str, timeout: float = 90.0) -> dict:
    if not config.ANTHROPIC_API_KEY:
        raise AssistantError("No API key — set the ANTHROPIC_API_KEY environment variable on the server (Render → Environment) and redeploy.")
    body = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM, "tools": TOOLS, "messages": messages}
    try:
        r = httpx.post(API_URL, json=body, timeout=timeout,
                       headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": API_VERSION, "content-type": "application/json"})
    except httpx.HTTPError as e:
        raise AssistantError(f"Could not reach the Claude API: {e}") from e
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:300])
        except ValueError:
            msg = r.text[:300]
        raise AssistantError(f"Claude API {r.status_code}: {msg}")
    return r.json()


def _history(db: Session, chat_id: int) -> list[dict]:
    rows = (db.query(models.AssistantMessage).filter_by(chat_id=chat_id)
            .order_by(models.AssistantMessage.id.desc()).limit(HISTORY_LIMIT).all())[::-1]
    # never start the window with a tool_result-only user turn (the API rejects it)
    while rows and rows[0].role == "user" and all(b.get("type") == "tool_result" for b in json.loads(rows[0].content or "[]")):
        rows = rows[1:]
    return [{"role": m.role, "content": json.loads(m.content or "[]")} for m in rows]


def _store(db: Session, chat_id: int, role: str, content: list) -> models.AssistantMessage:
    m = models.AssistantMessage(chat_id=chat_id, role=role, content=json.dumps(content, default=str))
    db.add(m)
    db.flush()
    return m


def send(db: Session, chat: models.AssistantChat, text: str, model: str) -> dict:
    """One operator turn: stores the user message, runs the tool loop, stores
    every assistant/tool message, returns {"blocks": [...]} for the page —
    text blocks, tool activity lines and approval cards, in order."""
    text = (text or "").strip()
    if not text:
        raise AssistantError("Say something first.")
    _store(db, chat.id, "user", [{"type": "text", "text": text}])
    if chat.title == "New chat":
        chat.title = text[:60]
    chat.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    out: list[dict] = []
    for _ in range(MAX_ROUNDS):
        resp = _api(_history(db, chat.id), model)
        content = resp.get("content") or []
        _store(db, chat.id, "assistant", content)
        db.commit()
        results = []
        for b in content:
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append({"type": "text", "text": b["text"]})
            elif b.get("type") == "tool_use":
                res_text, card = run_tool(db, b.get("name", ""), b.get("input") or {})
                out.append({"type": "tool", "name": b.get("name", ""), "args": b.get("input") or {}})
                if card:
                    out.append(card)
                results.append({"type": "tool_result", "tool_use_id": b.get("id"), "content": res_text})
        if resp.get("stop_reason") != "tool_use" or not results:
            break
        _store(db, chat.id, "user", results)
        db.commit()
    else:
        out.append({"type": "text", "text": "I stopped after several tool calls without a final answer — ask again more narrowly."})
    db.commit()
    return {"blocks": out}


def render_history(db: Session, chat_id: int) -> list[dict]:
    """Stored messages → page blocks (same shape `send` returns), for reload."""
    blocks: list[dict] = []
    for m in db.query(models.AssistantMessage).filter_by(chat_id=chat_id).order_by(models.AssistantMessage.id).all():
        content = json.loads(m.content or "[]")
        if m.role == "user":
            for b in content:
                if b.get("type") == "text":
                    blocks.append({"type": "me", "text": b["text"]})
                elif b.get("type") == "tool_result":
                    try:
                        res = json.loads(b.get("content") or "{}")
                    except ValueError:
                        res = {}
                    if isinstance(res, dict) and res.get("proposed") and res.get("actions"):
                        blocks.append({**res, "type": "actions", "stale": True})
        else:
            for b in content:
                if b.get("type") == "text" and b.get("text", "").strip():
                    blocks.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "tool_use":
                    blocks.append({"type": "tool", "name": b.get("name", ""), "args": b.get("input") or {}})
    return blocks
