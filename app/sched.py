"""What the background sweep is doing, step by step — for the Diagnostics › Scheduler card.

Each step the sweep enters (background.beat) is timed; a step that raised keeps its error;
the whole sweep's start, length and outcome are kept too. In memory only (one process,
one sweep thread) — nothing to write, nothing to clean. `due()` spaces out heavy steps that
shouldn't run on every slow cycle (the issue scan reads every ad of every account).
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
STEPS: dict[str, dict] = {}
SWEEP: dict = {"n": 0, "started": 0.0, "finished": 0.0, "dur": 0.0, "slow": False, "error": "", "interval": 0}
_cur: dict = {"name": "", "t0": 0.0}
_last_run: dict[str, float] = {}


def begin_sweep(n: int, slow: bool, interval: int, now: float | None = None) -> None:
    with _lock:
        SWEEP.update(n=n, started=now or time.time(), slow=slow, error="", interval=interval)
        _cur.update(name="", t0=0.0)


def step(name: str, now: float | None = None) -> None:
    """Close the running step (its duration) and open `name`."""
    now = now or time.time()
    with _lock:
        _close(now)
        _cur.update(name=name, t0=now)
        s = STEPS.setdefault(name, {"runs": 0, "last": 0.0, "dur": 0.0, "max": 0.0, "error": "", "error_at": 0.0})
        s["last"] = now


def _close(now: float) -> None:
    name = _cur.get("name")
    if not name:
        return
    s = STEPS.get(name)
    if s is not None:
        d = max(now - _cur["t0"], 0.0)
        s["runs"] += 1
        s["dur"] = round(d, 2)
        s["max"] = round(max(s["max"], d), 2)
    _cur.update(name="", t0=0.0)


def fail(name: str | None, exc) -> None:
    name = name or _cur.get("name") or "sweep"
    with _lock:
        s = STEPS.setdefault(name, {"runs": 0, "last": time.time(), "dur": 0.0, "max": 0.0, "error": "", "error_at": 0.0})
        s["error"], s["error_at"] = f"{type(exc).__name__}: {str(exc)[:200]}", time.time()


def current() -> str:
    return _cur.get("name") or ""


def end_sweep(error: str = "", now: float | None = None) -> None:
    now = now or time.time()
    with _lock:
        _close(now)
        SWEEP.update(finished=now, dur=round(now - (SWEEP["started"] or now), 1), error=error[:300])


def due(key: str, every_s: float, now: float | None = None) -> bool:
    """True (and remembered) when `key` last ran more than every_s ago — once after a restart."""
    now = now or time.time()
    with _lock:
        if now - _last_run.get(key, 0.0) < every_s:
            return False
        _last_run[key] = now
        return True


def snapshot(now: float | None = None) -> dict:
    now = now or time.time()
    with _lock:
        steps = [{"name": k, **v, "ago": round(now - v["last"]) if v["last"] else None,
                  "error_ago": round(now - v["error_at"]) if v.get("error_at") else None}
                 for k, v in sorted(STEPS.items(), key=lambda kv: -kv[1]["last"])]
        running = _cur.get("name") or ""
        return {"sweep": dict(SWEEP), "running": running, "running_for": round(now - _cur["t0"], 1) if running else 0,
                "steps": steps, "since_sweep": round(now - SWEEP["finished"]) if SWEEP["finished"] else None}
