"""Instant Pages page model (v132): one row per page NAME.

TikTok stores an Instant Page per ad account, so a page called "Benjamin" that lives on
62 accounts is 62 rows in the table. Presets and launches match pages by NAME, so the
operator thinks in names: "Benjamin — on 62 of 282 accounts, missing on 3 in BC X".
This module folds the per-account rows into that shape, adds the workspace's
favourite/tag marks, and produces the compact JSON the page filters and drawers run
on — so the template renders ~N name rows instead of ~N×accounts rows, and no row
carries a hidden 282-option account list (the old page shipped ~70,000 hidden
<option>s, which is why it was heavy).

Pure functions: no database, no request — the route passes rows in, tests pass stubs in.
"""
from __future__ import annotations

import json

NO_BC = ""          # accounts outside any Business Center group under this key


def parse_tag_ids(raw) -> list[int]:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out: list[int] = []
    for x in v if isinstance(v, list) else []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i not in out:
            out.append(i)
    return out


def group_pages(pages, accounts, bc_names: dict, marks: dict, tags_by_id: dict) -> dict:
    """pages: rows with .name .page_id .owner_advertiser_id .status .preview_url
    accounts: rows with .advertiser_id .advertiser_name .owner_bc_id (the view's enabled accounts)
    bc_names: bc_id → display name
    marks: page name → {"favorite": bool, "tag_ids": [ids]}      (this workspace's marks)
    tags_by_id: tag id → {"id", "name", "color"}                  (this workspace's tags)

    Returns {"groups": [...], "accounts": [...], "bcs": [...], "total": n} where each group is
      {name, count, published, drafts, favorite, tags, source, copies, bcs, missing_by_bc}
    sorted favourites first, then by name (case-insensitive)."""
    acct_list = [{"id": a.advertiser_id, "name": a.advertiser_name or a.advertiser_id, "bc": a.owner_bc_id or NO_BC}
                 for a in accounts]
    acct_bc = {a["id"]: a["bc"] for a in acct_list}
    by_bc: dict[str, list[str]] = {}
    for a in acct_list:
        by_bc.setdefault(a["bc"], []).append(a["id"])
    bcs = [{"bc_id": k, "name": (bc_names.get(k, k) if k else "No Business Center"), "n": len(v)} for k, v in by_bc.items()]
    bcs.sort(key=lambda b: (b["bc_id"] == NO_BC, b["name"].lower()))

    groups: dict[str, dict] = {}
    for p in pages:
        name = p.name or p.page_id
        g = groups.setdefault(name, {"name": name, "count": 0, "published": 0, "drafts": 0, "copies": [], "have": set()})
        status = (p.status or "").upper()
        g["count"] += 1
        if status == "PUBLISHED":
            g["published"] += 1
        else:
            g["drafts"] += 1
        g["copies"].append({"adv": p.owner_advertiser_id, "page_id": p.page_id, "status": status,
                            "preview": p.preview_url or "", "bc": acct_bc.get(p.owner_advertiser_id, NO_BC)})
        g["have"].add(p.owner_advertiser_id)

    out = []
    for name, g in groups.items():
        mark = marks.get(name) or {}
        tag_ids = [t for t in (mark.get("tag_ids") or []) if t in tags_by_id]
        # the copy a clone starts from: a PUBLISHED one (drafts can't be duplicated)
        src = next((c for c in g["copies"] if c["status"] == "PUBLISHED"), None)
        missing_by_bc = {bc: sum(1 for aid in ids if aid not in g["have"]) for bc, ids in by_bc.items()}
        out.append({
            "name": name, "count": g["count"], "published": g["published"], "drafts": g["drafts"],
            "favorite": bool(mark.get("favorite")), "tags": [tags_by_id[t] for t in tag_ids],
            "source": ({"page_id": src["page_id"], "adv": src["adv"]} if src else None),
            "copies": sorted(g["copies"], key=lambda c: c["adv"]),
            "bcs": sorted({c["bc"] for c in g["copies"]}),
            "bc_count": len({c["bc"] for c in g["copies"] if c["bc"]}),
            "missing_by_bc": missing_by_bc,
        })
    out.sort(key=lambda x: (not x["favorite"], x["name"].lower()))
    return {"groups": out, "accounts": acct_list, "bcs": bcs, "total": len(acct_list)}


def page_json(grouped: dict) -> dict:
    """What the page's JavaScript needs (no server-only fields)."""
    return {"total": grouped["total"], "accounts": grouped["accounts"], "bcs": grouped["bcs"],
            "groups": [{k: v for k, v in g.items() if k != "have"} for g in grouped["groups"]]}
