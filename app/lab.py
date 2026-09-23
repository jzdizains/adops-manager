"""Testing Lab — boards of experiments, each tied to the launches that ran it.

A board is one offer or question ("Offer X: which hook?"). A test on it says what it changes
(the variable), why (the hypothesis), which launch batches ran it, and — once decided — the
verdict (winner / failed / inconclusive) and the learning. Its numbers are read live from
those launches: campaigns, spend (saved day rows), revenue (postbacks by source), profit,
ROAS, how many are live, and TikTok's review verdicts. Nothing is copied; a test on a
deleted batch just shows fewer numbers. A test can be an iteration of another; the setup
difference (what the recipe changed) is worked out from the two batches' saved recipes.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

STATUSES = ("planned", "running", "winner", "failed", "inconclusive")
STATUS_LABEL = {"planned": "Planned", "running": "Running", "winner": "Winner", "failed": "Failed", "inconclusive": "Inconclusive"}
SETUP_KEYS = (("objective_type", "Objective"), ("destination_type", "Destination"), ("optimization_goal", "Goal"),
              ("optimization_event", "Event"), ("campaign_budget_mode", "Structure"), ("budget", "Budget"),
              ("cost_cap_ladder", "Cost cap"), ("bid_type", "Bidding"), ("location_ids", "Countries"),
              ("creative_source", "Creative"), ("template_name", "Preset"), ("duplicates", "Ad groups"))


def refs(raw: str) -> list[str]:
    """Batch refs from free text (commas, spaces, pasted result URLs). Pure."""
    out = []
    for tok in re.split(r"[\s,;]+", str(raw or "")):
        tok = tok.strip().rstrip("/")
        if "/campaigns/result/" in tok:
            tok = tok.split("/campaigns/result/", 1)[1].split("?")[0].split("/")[0]
        if tok and re.fullmatch(r"[A-Za-z0-9_-]{3,40}", tok) and tok not in out:
            out.append(tok)
    return out[:30]


def setup_of(recipe: dict) -> dict:
    """The recipe fields a person compares tests on, readable. Pure."""
    out = {}
    for key, label in SETUP_KEYS:
        v = recipe.get(key)
        if key == "budget":
            v = recipe.get("campaign_budget") or recipe.get("adgroup_budget")
        if v in (None, "", [], 0):
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v[:6]) + ("…" if len(v) > 6 else "")
        out[label] = str(v)[:60]
    return out


def setup_diff(a: dict, b: dict) -> list[str]:
    """Labels whose value differs between two setups (what an iteration changed). Pure."""
    return [label for _, label in SETUP_KEYS if (a.get(label) or "") != (b.get(label) or "")]


def rollup(rows: list[dict]) -> dict:
    """Totals over a test's campaigns. rows = [{spend, revenue, live}] — pure."""
    spend = round(sum(float(r.get("spend") or 0) for r in rows), 2)
    rev = round(sum(float(r.get("revenue") or 0) for r in rows), 2)
    return {"campaigns": len(rows), "live": sum(1 for r in rows if r.get("live")), "spend": spend, "revenue": rev,
            "profit": round(rev - spend, 2), "roas": round(rev / spend, 2) if spend else 0.0}


def recipe(db, queries, ref: str) -> dict:
    try:
        return json.loads(queries.get_setting(db, f"batch_fields:{ref}", "") or "{}")
    except ValueError:
        return {}


def metrics(db, models, test, sc=None, cache: dict | None = None) -> dict:
    """Live numbers for one test from its batches (only accounts in view). `cache` shares the
    source map and the year of postback revenue between the tests of one board."""
    from sqlalchemy import func
    from . import pnl_data, queries, review, timeutil
    rs = refs(test.batch_refs)
    logs = [lg for lg in (db.query(models.LaunchLog).filter(models.LaunchLog.batch_ref.in_(rs or [""])).all())
            if sc is None or sc.allows(lg.advertiser_id)]
    cids = [lg.campaign_id for lg in logs if lg.campaign_id]
    spend = {c: float(v or 0) for c, v in db.query(models.SpendSnapshot.campaign_id, func.sum(models.SpendSnapshot.spend))
             .filter(models.SpendSnapshot.campaign_id.in_(cids or [""])).group_by(models.SpendSnapshot.campaign_id)}
    live = {r.campaign_id for r in db.query(models.CampaignRecord.campaign_id, models.CampaignRecord.operation_status)
            .filter(models.CampaignRecord.campaign_id.in_(cids or [""])) if r.operation_status == "ENABLE"}
    cache = cache if cache is not None else {}
    if cids and "pb" not in cache:
        cache["srcmap"] = pnl_data.campaign_source_map(db)
        cache["pb"] = pnl_data.revenue_by_source(db, timeutil.local_midnight_utc(-365), timeutil.local_midnight_utc(1),
                                                 advertiser_ids=(sc.ids if sc is not None else None))   # this workspace's revenue only
    srcmap, pb = cache.get("srcmap", {}), cache.get("pb", {})
    rows = [{"spend": spend.get(c, 0.0), "revenue": float((pb.get(srcmap.get(c, "")) or {}).get("revenue", 0.0)), "live": c in live}
            for c in dict.fromkeys(cids)]
    out = rollup(rows)
    out["launched"] = sum(1 for lg in logs if lg.ok)
    out["failed"] = sum(1 for lg in logs if not lg.ok)
    out["review"] = review.summary(logs)
    # the recipe of a batch only when this view can see one of its launches (never someone else's)
    seen = next((r for r in rs if any(lg.batch_ref == r for lg in logs)), None)
    out["setup"] = setup_of(recipe(db, queries, seen)) if seen else {}
    out["refs"] = rs
    return out


def board_payload(db, models, board, sc=None) -> dict:
    tests = (db.query(models.LabTest).filter(models.LabTest.board_id == board.id)
             .order_by(models.LabTest.position, models.LabTest.id).all())
    items = []
    cache: dict = {}
    for t in tests:
        m = metrics(db, models, t, sc, cache)
        items.append({"id": t.id, "title": t.title, "hypothesis": t.hypothesis, "variable": t.variable, "status": t.status,
                      "learning": t.learning, "parent_id": t.parent_id, "batch_refs": ", ".join(m["refs"]), "m": m,
                      "updated": t.updated_at.isoformat() + "Z" if t.updated_at else ""})
    by_id = {i["id"]: i for i in items}
    for i in items:
        p = by_id.get(i["parent_id"])
        i["changed"] = setup_diff(p["m"]["setup"], i["m"]["setup"]) if p and p["m"]["setup"] and i["m"]["setup"] else []
        i["parent_title"] = p["title"] if p else ""
    return {"id": board.id, "title": board.title, "offer": board.offer, "description": board.description, "tests": items,
            "counts": {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}}


def touch(obj) -> None:
    obj.updated_at = datetime.utcnow()
