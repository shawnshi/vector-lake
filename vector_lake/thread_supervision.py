"""Liveness supervision for the watchdog's own loop threads.

The daemon runs its work on daemon threads -- outbox consumer, scheduled lint, ingest queue
worker, runner supervisor -- and the main thread is the only one that never exits.  Nothing
watched the others:

* the main thread rewrites the aggregate ``updated_at`` every 30 s, so the health surfaces
  (``runtime_health``, ``tool_doctor``) never saw a stale status file while the process lived;
* they read ``components[*].status`` but never a component's ``updated_at``;
* no code anywhere asked ``thread.is_alive()``.

A loop thread that died therefore left its last status ("idle") looking healthy for as long as
the process stayed up, and the plausible death path was not exotic: ``index_worker_loop`` ran
``close_connection()`` in a ``finally`` after its per-iteration ``except``, so an exception
there escaped the loop for good.

This module gives every loop one owner that can answer "is it still running", restarts it when
it is not, and reports a bounded failure when restarting does not hold.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("vector-lake-threads")

#: How many times one loop may be restarted in a process before it is reported as dead
#: instead.  A loop that dies immediately would otherwise restart on every heartbeat forever,
#: hiding the fault behind a hot restart loop.
DEFAULT_RESTART_LIMIT = 5

_lock = threading.Lock()
_loops: dict[str, dict] = {}


def start(name: str, factory: Callable[[], None], restart_limit: int = DEFAULT_RESTART_LIMIT) -> threading.Thread:
    """Start ``factory`` on a daemon thread named ``name`` and register it for supervision."""
    thread = threading.Thread(target=_run, args=(name, factory), name=name, daemon=True)
    with _lock:
        _loops[name] = {
            "factory": factory,
            "thread": thread,
            "restarts": 0,
            "restart_limit": int(restart_limit),
            "last_exit_reason": "",
            "finished": False,
        }
    thread.start()
    return thread


def _run(name: str, factory: Callable[[], None]) -> None:
    """Run a loop body and record how it ended.

    A normal return is as much a death as an exception: the loop contract is "run until the
    process ends", so either way the supervisor has to be able to tell that it happened.
    """
    try:
        factory()
        reason = "returned"
    except BaseException as exc:  # noqa: BLE001 - recording the death is the whole point
        reason = f"{type(exc).__name__}: {exc}"
        log.error("Loop thread %s died: %s", name, reason, exc_info=True)
    with _lock:
        entry = _loops.get(name)
        if entry is not None:
            entry["last_exit_reason"] = reason


def finish(name: str, reason: str = "") -> None:
    """Declare that a loop ended because its work is done, not because it died.

    Some loops are *supposed* to return: catch-up returns when
    ``VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS=0`` disables it, and the runner supervisor returns when
    the host opts out of ingestion or the supervisor script is absent.  Treating those returns as
    deaths restarted them on every heartbeat and, after the restart budget, reported the daemon as
    ``error`` -- on a configuration both modules document as supported (independent review,
    2026-09-19).
    """
    with _lock:
        entry = _loops.get(name)
        if entry is not None:
            entry["finished"] = True
            entry["last_exit_reason"] = reason or "finished"


def supervise_once() -> dict[str, str]:
    """Restart any registered loop that is no longer running.

    Returns ``{name: "alive" | "restarted" | "dead"}`` -- ``"dead"`` meaning the restart
    budget is spent, which is the state the caller must surface rather than swallow.
    """
    outcome: dict[str, str] = {}
    with _lock:
        entries = list(_loops.items())
    for name, entry in entries:
        if entry.get("finished") and not (entry["thread"] and entry["thread"].is_alive()):
            # Ended on purpose: nothing to restart, and nothing to report as a fault.
            outcome[name] = "finished"
            continue
        thread: threading.Thread | None = entry["thread"]
        if thread is not None and thread.is_alive():
            outcome[name] = "alive"
            continue
        reason = entry.get("last_exit_reason") or "not running"
        if int(entry["restarts"]) >= int(entry["restart_limit"]):
            outcome[name] = "dead"
            continue
        replacement = threading.Thread(
            target=_run, args=(name, entry["factory"]), name=name, daemon=True
        )
        with _lock:
            current = _loops.get(name)
            if current is None:
                continue
            current["thread"] = replacement
            current["restarts"] = int(current["restarts"]) + 1
            current["last_exit_reason"] = ""
            restarts = current["restarts"]
            limit = current["restart_limit"]
        log.error(
            "Loop thread %s was not running (%s); restarted as %s (%d/%d)",
            name, reason, replacement.name, restarts, limit,
        )
        replacement.start()
        outcome[name] = "restarted"
    return outcome


def snapshot() -> dict[str, dict]:
    """Per-loop state for a status surface."""
    with _lock:
        return {
            name: {
                "alive": bool(entry["thread"] is not None and entry["thread"].is_alive()),
                "restarts": int(entry["restarts"]),
                "restart_limit": int(entry["restart_limit"]),
                "finished": bool(entry.get("finished")),
                "last_exit_reason": entry.get("last_exit_reason") or "",
            }
            for name, entry in _loops.items()
        }


def describe(outcome: dict[str, str]) -> tuple[str, str]:
    """``(state, message)`` for a status write, given a :func:`supervise_once` result."""
    dead = sorted(name for name, result in outcome.items() if result == "dead")
    restarted = sorted(name for name, result in outcome.items() if result == "restarted")
    if dead:
        return "error", "loop thread(s) dead, restart budget spent: " + ", ".join(dead)
    if restarted:
        return "processing", "restarted loop thread(s): " + ", ".join(restarted)
    finished = sorted(name for name, result in outcome.items() if result == "finished")
    if finished and len(finished) == len(outcome):
        return "idle", "loop thread(s) finished by design: " + ", ".join(finished)
    if finished:
        return "idle", (
            f"{len(outcome) - len(finished)} loop thread(s) alive; "
            f"finished by design: " + ", ".join(finished)
        )
    return "idle", f"{len(outcome)} loop thread(s) alive"


def reset() -> None:
    """Forget every registration.  Tests only; a process owns one registry."""
    with _lock:
        _loops.clear()
