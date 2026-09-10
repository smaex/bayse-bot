"""Small in-process health registry used by the bot and HTTP probes.

Liveness (the event loop can answer) and readiness (the trading engine is making
progress) are intentionally different.  A process that only answers ``/ping``
can be alive while every trading task is dead.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_started_at = time.time()
_components: dict[str, dict[str, Any]] = {}
_declared_ready = False


def set_ready(value: bool) -> None:
    """Declare whether startup/recovery has completed."""
    global _declared_ready
    with _lock:
        _declared_ready = bool(value)


def touch(name: str, **details: Any) -> None:
    """Record successful progress for a component."""
    now = time.time()
    with _lock:
        previous = _components.get(name, {})
        _components[name] = {
            **previous,
            **details,
            "last_ok": now,
            "last_error": "",
            "error_at": 0.0,
        }


def fail(name: str, error: object, **details: Any) -> None:
    """Record a component failure without erasing its last success time."""
    now = time.time()
    with _lock:
        previous = _components.get(name, {})
        _components[name] = {
            **previous,
            **details,
            "last_ok": float(previous.get("last_ok", 0.0)),
            "last_error": str(error)[:500],
            "error_at": now,
        }


def remove(name: str) -> None:
    with _lock:
        _components.pop(name, None)


def snapshot() -> dict[str, Any]:
    now = time.time()
    with _lock:
        components = {name: dict(value) for name, value in _components.items()}
    for value in components.values():
        last_ok = float(value.get("last_ok", 0.0))
        value["age_sec"] = round(now - last_ok, 3) if last_ok else None
    return {
        "uptime_sec": round(now - _started_at, 3),
        "components": components,
    }


def readiness(
    *,
    main_max_age: float = 30.0,
    lock_max_age: float = 45.0,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Return readiness and reasons suitable for an HTTP health endpoint."""
    data = snapshot()
    components = data["components"]
    reasons: list[str] = []

    with _lock:
        declared_ready = _declared_ready
    if not declared_ready:
        reasons.append("startup has not completed")

    for name, max_age in (("bot", main_max_age), ("singleton_lock", lock_max_age)):
        item = components.get(name)
        if not item or item.get("age_sec") is None:
            reasons.append(f"{name} has not reported")
        elif float(item["age_sec"]) > max_age:
            reasons.append(f"{name} stale for {item['age_sec']:.1f}s")
        elif item.get("last_error") and float(item.get("error_at", 0)) > float(item.get("last_ok", 0)):
            reasons.append(f"{name} failed: {item['last_error']}")

    data["ready"] = not reasons
    data["reasons"] = reasons
    return not reasons, reasons, data
