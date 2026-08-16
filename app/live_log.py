"""In-memory live event feed for the Performance page (conversion dings etc.).

Deliberately tiny: a ring buffer the poller endpoint reads. Real revenue truth
lives in ConversionSample rows; this is just the 'something happened' stream.
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from datetime import datetime, timezone

_lock = threading.Lock()
_events: deque[dict] = deque(maxlen=200)
_counter = itertools.count(1)


def push(kind: str, message: str, **extra):
    with _lock:
        _events.append({
            "id": next(_counter),
            "kind": kind,           # conversion | launch | error | info
            "message": message,
            "at": datetime.now(timezone.utc).isoformat(),
            **extra,
        })


def since(last_id: int = 0) -> list[dict]:
    with _lock:
        return [e for e in _events if e["id"] > last_id]
