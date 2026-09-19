"""The scheduled occurrence, run for real rather than asserted from its source.

The pieces were covered -- the lint is outside the mutex, the retry is bounded, the maintenance
chain is outside the lint's try -- but only by reading the source with ``inspect``.  This runs one
occurrence against an isolated root with the expensive parts stubbed, so the *wiring* is exercised:
the order, the fact that a failing lint still runs the maintenance, and the occurrence marker.

The loop sleeps 30 s between ticks, so ``watchdog_app.time.sleep`` is replaced with a signal that
ends the thread after the first iteration.  ``_Stop`` derives from ``BaseException`` on purpose: the
loop catches ``Exception`` per iteration and would otherwise swallow it.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from vector_lake import backup_retention, db_store, indexer, memory_gram_index, tool_lint, watchdog_app
from vector_lake.wiki_utils import get_meta_dir


class _Stop(BaseException):
    """Ends the loop thread after its first iteration."""


def _state() -> dict:
    path = get_meta_dir() / ".scheduled_lint_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _run_occurrences(
    monkeypatch,
    occurrence: str,
    ticks: int = 1,
    lint_raises: bool = False,
    ignore_state: bool = False,
) -> dict:
    """Run `scheduled_lint_loop` for ``ticks`` iterations, recording what it did.

    ``watchdog_app.time.sleep`` is replaced by a signal that ends the thread after the requested
    number of iterations; ``_Stop`` derives from ``BaseException`` because the loop's own
    ``except Exception`` would swallow an ordinary error and it would simply keep going.
    """
    calls: dict[str, list] = {"lint": [], "graph": [], "gram": [], "retention": []}

    def _lint(*args, **kwargs):
        calls["lint"].append(kwargs.get("auto_fix"))
        if lint_raises:
            raise RuntimeError("lint exploded")

    monkeypatch.setattr(tool_lint, "lint_vector_lake", _lint)
    monkeypatch.setattr(
        indexer, "refresh_graph_topology_if_dirty", lambda: calls["graph"].append(True)
    )
    monkeypatch.setattr(
        memory_gram_index, "maybe_rebuild_memory_gram_index",
        lambda *a, **k: calls["gram"].append(True) or "gram: not due",
    )
    monkeypatch.setattr(memory_gram_index, "rebuild_due", lambda *a, **k: False)
    monkeypatch.setattr(
        backup_retention, "prune_backups",
        lambda *a, **k: calls["retention"].append(True)
        or {"deleted": [], "keep": [], "failures": [], "removable_bytes": 0},
    )
    monkeypatch.setattr(watchdog_app, "scheduled_lint_occurrence", lambda now: occurrence)
    if ignore_state:
        monkeypatch.setenv("VECTOR_LAKE_IGNORE_SCHEDULED_LINT_STATE", "1")
    else:
        monkeypatch.delenv("VECTOR_LAKE_IGNORE_SCHEDULED_LINT_STATE", raising=False)

    remaining = {"ticks": ticks}

    def _sleep(_seconds):
        remaining["ticks"] -= 1
        if remaining["ticks"] <= 0:
            raise _Stop()
        return None

    monkeypatch.setattr(watchdog_app.time, "sleep", _sleep)

    finished = threading.Event()

    def _run():
        try:
            watchdog_app.scheduled_lint_loop()
        except _Stop:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=_run, name="scheduled-lint", daemon=True)
    worker.start()
    assert finished.wait(20), "the occurrence never reached its sleep"
    return calls


@pytest.fixture
def daemon_root(isolated_memory):
    db_store.init_db()
    return isolated_memory


def test_an_occurrence_runs_the_lint_and_the_maintenance_chain(daemon_root, monkeypatch):
    calls = _run_occurrences(monkeypatch, "2026-09-19-10")

    assert calls["lint"] == [False], "the lint did not run with auto_fix disabled"
    assert calls["graph"] == [True], "the graph refresh did not run"
    assert calls["gram"] == [True], "the gram maintenance did not run"
    assert calls["retention"] == [True], "retention did not run"
    assert _state()["last_occurrence"] == "2026-09-19-10"
    assert _state()["outcome"] == "completed"


def test_a_completed_occurrence_is_not_repeated_on_the_next_tick(daemon_root, monkeypatch):
    """The marker is what stops a completed occurrence being re-run on every tick."""
    _run_occurrences(monkeypatch, "2026-09-19-10")
    calls = _run_occurrences(monkeypatch, "2026-09-19-10")

    assert calls["lint"] == [], "a completed occurrence ran again"
    assert calls["gram"] == []
    assert calls["graph"] == []


def test_a_failing_lint_still_runs_the_maintenance(daemon_root, monkeypatch):
    """S3: the WAL checkpoint, the rebuild and retention must not be in the lint's blast radius.

    A failing occurrence used to skip everything after it -- including the only periodic WAL
    truncation -- and retry every 30 s while holding ``global_task_lock``.
    """
    calls = _run_occurrences(monkeypatch, "2026-09-19-23", ticks=1, lint_raises=True)

    assert calls["lint"] == [False]
    assert calls["gram"] == [True], "a failing lint skipped the gram maintenance"
    assert calls["retention"] == [True], "a failing lint skipped retention"
    assert "last_occurrence" not in _state(), "a single failure was recorded as an outcome"


def test_a_permanently_failing_occurrence_is_bounded_and_recorded_as_failed(daemon_root, monkeypatch):
    """Three failed ticks, then the occurrence is given up on rather than retried forever."""
    calls = _run_occurrences(
        monkeypatch, "2026-09-19-23", ticks=watchdog_app.MAX_OCCURRENCE_ATTEMPTS, lint_raises=True
    )

    assert len(calls["lint"]) == watchdog_app.MAX_OCCURRENCE_ATTEMPTS
    assert len(calls["retention"]) == watchdog_app.MAX_OCCURRENCE_ATTEMPTS, (
        "the maintenance chain is part of every attempt, not only the successful one"
    )
    state = _state()
    assert state["last_occurrence"] == "2026-09-19-23"
    assert state["outcome"] == "failed"
    assert "lint exploded" in state["detail"]


def test_a_recovered_occurrence_is_recorded_as_completed(daemon_root, monkeypatch):
    """After failures, the next tick that succeeds must clear the failed state."""
    _run_occurrences(monkeypatch, "2026-09-19-23", ticks=1, lint_raises=True)
    calls = _run_occurrences(monkeypatch, "2026-09-19-23", ticks=1)

    assert calls["lint"] == [False], "the occurrence was not retried after a failure"
    state = _state()
    assert state["outcome"] == "completed"
    assert state["last_occurrence"] == "2026-09-19-23"
