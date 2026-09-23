"""Numbered database migrations (v153).

init_db already makes the schema (new tables, missing columns, missing indexes). What it can't
do is change DATA or run a one-off fix exactly once — that's what this is: a `schema_migrations`
table records every numbered step applied (number, name, when, how long, rows touched), and
`run()` applies the missing ones in order at boot, each in its own transaction, logging a failed
step and stopping there (the app still starts; the step is retried next boot).

Rules for a step: never delete user data; be safe to re-run (every step is written so that a
second run changes nothing); keep it fast (it runs at boot). Add a step by appending to STEPS
with the next number — never renumber, never edit a shipped step.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

log = logging.getLogger("adops.migrations")

TABLE = "schema_migrations"


def _baseline(conn) -> int:
    """v153: the schema as init_db leaves it is the starting point."""
    return 0


def _geo_policy_default(conn) -> int:
    """v151 added ad_accounts.geo_policy — rows that predate it read 'any' (the default)."""
    return conn.exec_driver_sql("UPDATE ad_accounts SET geo_policy = 'any' WHERE geo_policy IS NULL OR geo_policy = ''").rowcount or 0


def _rule_alerts_to_accounts(conn) -> int:
    """Rule-engine alerts were filed under the CAMPAIGN id, which hid them from every buyer's Inbox
    and Telegram (the v151 audit). Re-file the old ones under their ad account."""
    return conn.exec_driver_sql(
        "UPDATE alerts SET ref_id = (SELECT c.advertiser_id FROM campaign_records c WHERE c.campaign_id = alerts.ref_id LIMIT 1) "
        "WHERE kind = 'rule_action' AND ref_id IN (SELECT campaign_id FROM campaign_records)").rowcount or 0


def _profile_posts_via(conn) -> int:
    """v152: stored profile posts from before the code-identity merge came through the BC."""
    return conn.exec_driver_sql("UPDATE profile_posts SET via = 'bc' WHERE via IS NULL OR via = ''").rowcount or 0


def _status_changed_naive(conn) -> int:
    """The account status listener once stamped an aware time; SQLite keeps the text with its
    '+00:00'. Normalise to the naive UTC every other stored time uses."""
    return conn.exec_driver_sql(
        "UPDATE ad_accounts SET status_changed_at = replace(status_changed_at, '+00:00', '') "
        "WHERE status_changed_at LIKE '%+00:00'").rowcount or 0


STEPS = [
    (1, "baseline", _baseline),
    (2, "ad_accounts.geo_policy defaults to 'any'", _geo_policy_default),
    (3, "rule alerts filed under their ad account", _rule_alerts_to_accounts),
    (4, "profile_posts.via for posts stored before the merge", _profile_posts_via),
    (5, "ad_accounts.status_changed_at as naive UTC", _status_changed_naive),
]


def pending(applied: set, steps=None) -> list:
    """The steps not applied yet, in number order. Pure."""
    steps = steps if steps is not None else STEPS
    nums = [n for n, _, _ in steps]
    assert nums == sorted(nums) and len(set(nums)) == len(nums), "migration numbers must be unique and ascending"
    return [s for s in steps if s[0] not in applied]


def _ensure_table(conn) -> None:
    conn.exec_driver_sql(f"CREATE TABLE IF NOT EXISTS {TABLE} (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                         "applied_at TEXT NOT NULL, ms INTEGER DEFAULT 0, rows INTEGER DEFAULT 0)")


def applied(engine) -> list[dict]:
    with engine.begin() as conn:
        _ensure_table(conn)
        return [{"version": r[0], "name": r[1], "applied_at": r[2], "ms": r[3], "rows": r[4]}
                for r in conn.exec_driver_sql(f"SELECT version, name, applied_at, ms, rows FROM {TABLE} ORDER BY version")]


def run(engine, steps=None) -> dict:
    """Apply every pending step, in order, each in its own transaction. Stops at the first
    failure (later steps may depend on it). Returns {applied: [...], failed: name|None}."""
    with engine.begin() as conn:
        _ensure_table(conn)
        done = {r[0] for r in conn.exec_driver_sql(f"SELECT version FROM {TABLE}")}
    out: dict = {"applied": [], "failed": None}
    for num, name, fn in pending(done, steps):
        t0 = time.time()
        try:
            with engine.begin() as conn:
                n = int(fn(conn) or 0)
                conn.exec_driver_sql(f"INSERT INTO {TABLE} (version, name, applied_at, ms, rows) VALUES (?, ?, ?, ?, ?)",
                                     (num, name, datetime.utcnow().isoformat(timespec="seconds"), int((time.time() - t0) * 1000), n))
            out["applied"].append({"version": num, "name": name, "rows": n})
            log.info("migration %s applied: %s (%s rows)", num, name, n)
        except Exception as e:      # noqa: BLE001 — never a boot blocker; retried next start
            out["failed"] = f"{num} {name}: {type(e).__name__}: {str(e)[:200]}"
            log.exception("migration %s failed: %s", num, name)
            break
    return out
