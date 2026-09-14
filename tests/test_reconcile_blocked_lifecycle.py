"""P0 regression tests: bounded, honest, non-starving Wiki reconciliation.

These cover the four P0 items from the 2026-09-14 reliability audit:

* P0-1 a zero-progress verification round must not re-claim the generation
  immediately, because the plan is re-derived from the same scan.
* P0-2 consecutive zero-progress rounds must pause, then stop claiming and
  report ``reconcile_blocked`` while retaining the durable marker.
* P0-3 ``reconcile_blocked`` must stay an explicit, operator-visible issue
  rather than being reported as a worker crash.
* P0-4 the topology max-staleness cap must be self-enforcing, i.e. armed from a
  published projection pair instead of from projection progress.
"""

from __future__ import annotations

import os
import queue
import time
from datetime import datetime, timezone

from vector_lake import db_store, watchdog_app
from vector_lake.runtime_health import _watchdog_component_health
from vector_lake.tool_doctor import _doctor_watchdog_status
from vector_lake.watchdog_app import WikiIndexEventBuffer, reconcile_wiki_overflow_once
from vector_lake.watchdog_status import get_status_file, write_status


_ZERO_PROGRESS_ROUND = {
    "generation": 1,
    "selected": 1,
    "completed": 0,
    "failed": 0,
    "remaining": 1,
    "scan_errors": [],
    "generation_changed": False,
    "cleared": False,
}


def _overflowing_buffer() -> WikiIndexEventBuffer:
    events = WikiIndexEventBuffer(max_pending=1)
    assert events.put("Concept_Queued.md") is True
    assert events.put("Concept_Overflow.md") is False
    assert events.get_nowait() == "Concept_Queued.md"
    events.task_done()
    return events


# --------------------------------------------------------------------------- #
# P0-1
# --------------------------------------------------------------------------- #


def test_zero_progress_verification_round_does_not_rearm_reconcile(monkeypatch):
    events = _overflowing_buffer()
    generation = events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=100,
    )
    assert generation == 1

    # The scan keeps reporting the same page while the repair path quarantines it,
    # so the round repairs nothing and reports no failure.
    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: {
            "candidates": ["Concept_Poison.md"],
            "errors": [],
            "total_drift": 1,
        },
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: {
            "completed": 0,
            "failed": 0,
            "quarantined": len(filenames),
        },
    )

    result = reconcile_wiki_overflow_once(events, generation, batch_size=25)

    assert result["completed"] == 0
    assert result["failed"] == 0
    assert result["scan_errors"] == []
    assert result["remaining"] == 1
    assert result["cleared"] is False
    # The obligation is retained, but it must not be re-claimable before the
    # configured retry interval: that immediate re-claim is what turned the loop
    # into an unbounded full-Wiki rescan while holding the shared heavy-task gate.
    assert (
        events.claim_full_reconcile_marker(retry_interval_seconds=30, now=101) is None
    )
    assert (
        events.claim_full_reconcile_marker(retry_interval_seconds=30, now=131)
        == generation
    )


def test_progress_round_still_rearms_reconcile_immediately(monkeypatch):
    events = _overflowing_buffer()
    generation = events.claim_full_reconcile_marker(
        retry_interval_seconds=30,
        now=100,
    )
    assert generation == 1

    monkeypatch.setattr(
        "vector_lake.watchdog_app._scan_wiki_reconcile_plan",
        lambda limit: {
            "candidates": ["Concept_A.md", "Concept_B.md"],
            "errors": [],
            "total_drift": 2,
        },
    )
    monkeypatch.setattr(
        "vector_lake.watchdog_app.process_legacy_projection_batch",
        lambda filenames: {"completed": len(filenames), "failed": 0},
    )

    result = reconcile_wiki_overflow_once(events, generation, batch_size=25)

    assert result["completed"] == 2
    assert result["cleared"] is False
    # Real progress must keep draining without waiting out the retry interval.
    assert (
        events.claim_full_reconcile_marker(retry_interval_seconds=30, now=101)
        == generation
    )


# --------------------------------------------------------------------------- #
# P0-2
# --------------------------------------------------------------------------- #


class _AlwaysOverflowingQueue:
    """Queue whose full-reconcile obligation never clears."""

    full_reconcile_required = True
    max_pending = 1

    def __init__(self) -> None:
        self.claims = 0

    @staticmethod
    def qsize() -> int:
        return 0

    @staticmethod
    def empty() -> bool:
        return True

    @staticmethod
    def get_nowait():
        raise queue.Empty

    @staticmethod
    def task_done() -> None:
        return None

    def claim_full_reconcile_marker(self, **_kwargs) -> int:
        self.claims += 1
        return 1


class _StopAfterWaits:
    """Stop event that records waits and stops on the Nth one."""

    def __init__(self, stop_on_call: int) -> None:
        self.stop_on_call = stop_on_call
        self.waits: list[float] = []
        self.calls = 0

    @staticmethod
    def is_set() -> bool:
        return False

    def wait(self, seconds) -> bool:
        self.waits.append(seconds)
        self.calls += 1
        return self.calls >= self.stop_on_call


def _drive_outbox_loop(
    monkeypatch,
    queue_state,
    statuses,
    stop,
    *,
    claimable: bool = False,
) -> None:
    monkeypatch.setattr(watchdog_app, "index_queue", queue_state)
    monkeypatch.setattr(
        db_store, "mutation_outbox_has_claimable", lambda: claimable
    )
    monkeypatch.setattr(db_store, "close_connection", lambda: None)
    monkeypatch.setattr(
        watchdog_app,
        "process_mutation_outbox_batch",
        lambda **_kwargs: {
            "claimed": 0,
            "completed": 0,
            "retrying": 0,
            "failed": 0,
        },
    )
    monkeypatch.setattr(
        watchdog_app,
        "reconcile_wiki_overflow_once",
        lambda *_args, **_kwargs: dict(_ZERO_PROGRESS_ROUND),
    )
    monkeypatch.setattr(
        watchdog_app,
        "write_status",
        lambda state, *_args, **_kwargs: statuses.append(state),
    )
    watchdog_app.index_worker_loop(stop)


def test_zero_progress_round_pauses_instead_of_spinning(
    isolated_memory,
    monkeypatch,
):
    statuses: list[str] = []
    queue_state = _AlwaysOverflowingQueue()
    monkeypatch.setenv(
        "VECTOR_LAKE_WIKI_RECONCILE_ZERO_PROGRESS_POLL_SECONDS",
        "0.25",
    )

    _drive_outbox_loop(monkeypatch, queue_state, statuses, _StopAfterWaits(1))

    # Without this pause the consumer had no wait at all on a zero-progress round
    # while the claim throttle was still in effect.
    assert statuses[:3] == ["idle", "processing", "stopped"]
    assert queue_state.claims == 1


def test_zero_progress_reconcile_blocks_and_stops_claiming(
    isolated_memory,
    monkeypatch,
):
    statuses: list[str] = []
    queue_state = _AlwaysOverflowingQueue()
    stop = _StopAfterWaits(4)

    _drive_outbox_loop(monkeypatch, queue_state, statuses, stop)

    assert queue_state.claims == watchdog_app._WIKI_RECONCILE_ZERO_PROGRESS_ROUNDS
    assert "reconcile_blocked" in statuses
    # Once blocked the loop must keep reporting the blocked condition instead of
    # falling back to a bare "idle" that would hide it.
    assert statuses.count("reconcile_blocked") >= 2
    assert statuses[-1] == "stopped"
    assert all(seconds > 0 for seconds in stop.waits)


def test_blocked_reconcile_does_not_admit_the_heavy_task_gate(
    isolated_memory,
    monkeypatch,
):
    """A settled blocked condition must not keep taking the shared gate."""
    acquisitions = {"count": 0}

    class _FreeLease:
        def __enter__(self):
            return self

        @staticmethod
        def __exit__(*_args):
            return False

    def _acquire(*_args, **_kwargs):
        acquisitions["count"] += 1
        return _FreeLease()

    monkeypatch.setattr("vector_lake.heavy_task_gate.heavy_task", _acquire)

    statuses: list[str] = []
    queue_state = _AlwaysOverflowingQueue()
    _drive_outbox_loop(monkeypatch, queue_state, statuses, _StopAfterWaits(5))

    # Rounds 1-3 take the gate to attempt repair; once blocked, the obligation
    # alone must not admit the gate any more.
    assert acquisitions["count"] == watchdog_app._WIKI_RECONCILE_ZERO_PROGRESS_ROUNDS
    assert statuses[-1] == "stopped"
    first_blocked = statuses.index("reconcile_blocked")
    assert "idle" not in statuses[first_blocked:]


def test_gate_deferral_does_not_clear_a_blocked_condition(
    isolated_memory,
    monkeypatch,
):
    """A transient cycle report must not flap a settled blocked condition."""
    from vector_lake.heavy_task_gate import HeavyTaskBusy

    class _FreeLease:
        def __enter__(self):
            return self

        @staticmethod
        def __exit__(*_args):
            return False

    class _BusyLease:
        def __enter__(self):
            raise HeavyTaskBusy(
                task_class="projection",
                operation="competing-heavy-task",
                origin="test",
                wait_timeout_seconds=0.0,
                gate_status={},
                waited_seconds=0.0,
            )

        @staticmethod
        def __exit__(*_args):
            return False

    acquisitions = {"count": 0}

    def _acquire(*_args, **_kwargs):
        acquisitions["count"] += 1
        # Rounds 1-3 run normally and settle into the blocked state; round 4 is
        # deferred by a competing heavy task, which previously rewrote "idle".
        return _BusyLease() if acquisitions["count"] >= 4 else _FreeLease()

    monkeypatch.setattr("vector_lake.heavy_task_gate.heavy_task", _acquire)

    statuses: list[str] = []
    queue_state = _AlwaysOverflowingQueue()
    # A claimable outbox row is real work, so the gate is admitted even while the
    # reconcile obligation is blocked -- which is what makes the deferral path
    # reachable and proves a transient report cannot rewrite the state.
    _drive_outbox_loop(
        monkeypatch,
        queue_state,
        statuses,
        _StopAfterWaits(4),
        claimable=True,
    )

    assert "reconcile_blocked" in statuses
    first_blocked = statuses.index("reconcile_blocked")
    assert "idle" not in statuses[first_blocked:]
    assert acquisitions["count"] >= 4


# --------------------------------------------------------------------------- #
# P0-3
# --------------------------------------------------------------------------- #


def _full_profile_status(now: datetime, outbox_status: str, last_error: str = ""):
    components = (
        "watchdog",
        "outbox",
        "scheduler",
        "ingest",
        "auto_ingest",
    )
    status = {
        "schema_version": 4,
        "run_id": "run-1",
        "process_id": os.getpid(),
        "expected_components": list(components),
        "updated_at": now.isoformat(),
        "run_profile": {
            "schema_version": 1,
            "name": "full",
            "components": list(components),
        },
        "components": {
            name: {
                "status": "idle",
                "heartbeat_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "run_id": "run-1",
                "process_id": os.getpid(),
            }
            for name in components
        },
        "status": "idle",
    }
    status["components"]["outbox"]["status"] = outbox_status
    status["components"]["outbox"]["last_error"] = last_error
    status["status"] = outbox_status
    return status


def test_reconcile_blocked_is_not_classified_as_a_worker_fault():
    now = datetime.now(timezone.utc)
    status = _full_profile_status(
        now,
        "reconcile_blocked",
        "remaining=2; failed=0; quarantine=2 quarantined manual edit(s)",
    )

    health = _watchdog_component_health(
        status,
        now_utc=now,
        component_max_age=120,
        auto_ingest_enabled=False,
    )

    assert health["blocked_required_components"] == ["outbox"]
    assert health["blocked_optional_components"] == []
    assert "outbox" not in health["unhealthy_required_components"]
    assert health["aggregate_requires_block"] is False
    assert "remaining=2" in health["blocked_component_details"]["outbox"]


def test_reconcile_blocked_still_visible_in_doctor_watchdog_detail():
    now = datetime.now(timezone.utc)
    status = _full_profile_status(now, "reconcile_blocked", "remaining=2")

    result, detail = _doctor_watchdog_status(
        status,
        now_utc=now,
        component_max_age=120,
        auto_ingest_enabled=False,
    )

    # Degraded, not blocking: the worker is alive, so the reason is reported and
    # the separate runtime-health issue carries the operator decision.
    assert result is None
    assert "blocked=outbox" in detail
    assert "unhealthy=none" in detail


def test_reconcile_blocked_keeps_an_explicit_runtime_health_issue(isolated_memory):
    db_store.init_db()
    for component in ("watchdog", "ingest"):
        write_status("idle", 0, 0, f"{component} heartbeat", "", component=component)
    write_status(
        "reconcile_blocked",
        0,
        0,
        "Full Wiki reconciliation blocked; operator action required",
        "remaining=2; failed=0; quarantine=2",
        component="outbox",
    )

    status = get_status_file().read_text(encoding="utf-8")
    assert '"status": "reconcile_blocked"' in status

    from vector_lake.runtime_health import assess_runtime_health

    health = assess_runtime_health()

    assert health["ok"] is False
    assert any(
        issue.startswith("wiki_reconcile_blocked:outbox") for issue in health["issues"]
    )
    # The generic crash label must no longer be used for this condition.
    assert not any(
        issue.startswith("watchdog_unhealthy") for issue in health["issues"]
    )


# --------------------------------------------------------------------------- #
# P0-4
# --------------------------------------------------------------------------- #


def test_topology_source_token_tracks_projection_publish(isolated_memory):
    path = isolated_memory / "wiki" / "projection_pair_manifest.json"
    assert watchdog_app._topology_source_token() is None

    path.write_text('{"projection_generation": "a"}', encoding="utf-8")
    first = watchdog_app._topology_source_token()
    assert first is not None

    # index.json is a format-2 locator and never changes on publish, so the
    # manifest is the only usable freshness signal.
    path.write_text(
        '{"projection_generation": "bb"}',
        encoding="utf-8",
    )
    assert watchdog_app._topology_source_token() != first


class _SleepThenStop:
    """Stop event that sleeps so a debounce deadline can elapse."""

    def __init__(self, sleeps: list[float]) -> None:
        self.sleeps = list(sleeps)
        self.waits: list[float] = []

    @staticmethod
    def is_set() -> bool:
        return False

    def wait(self, seconds) -> bool:
        self.waits.append(seconds)
        time.sleep(self.sleeps.pop(0))
        return not self.sleeps


def _drive_topology_cap(monkeypatch, isolated_memory, *, with_manifest: bool) -> int:
    from vector_lake import indexer

    if with_manifest:
        (
            isolated_memory / "wiki" / "projection_pair_manifest.json"
        ).write_text('{"projection_generation": "a"}', encoding="utf-8")

    refresh_calls: list[bool] = []
    monkeypatch.setenv("VECTOR_LAKE_TOPOLOGY_REFRESH_DEBOUNCE_SECONDS", "0.1")
    monkeypatch.setenv("VECTOR_LAKE_TOPOLOGY_MAX_STALENESS_SECONDS", "30")
    monkeypatch.setattr(
        indexer,
        "refresh_graph_topology_if_dirty",
        lambda **_kwargs: refresh_calls.append(True) or True,
    )
    monkeypatch.setattr(watchdog_app, "index_queue", _AlwaysOverflowingQueue())
    monkeypatch.setattr(db_store, "mutation_outbox_has_claimable", lambda: False)
    monkeypatch.setattr(db_store, "close_connection", lambda: None)
    monkeypatch.setattr(watchdog_app, "write_status", lambda *_a, **_k: None)
    monkeypatch.setattr(
        watchdog_app,
        "process_mutation_outbox_batch",
        lambda **_kwargs: {
            "claimed": 0,
            "completed": 0,
            "retrying": 0,
            "failed": 0,
        },
    )
    # No reconcile claim, so projection_completed stays false for every round.
    monkeypatch.setattr(
        watchdog_app,
        "reconcile_wiki_overflow_once",
        lambda *_args, **_kwargs: dict(_ZERO_PROGRESS_ROUND),
    )
    monkeypatch.setattr(
        _AlwaysOverflowingQueue,
        "claim_full_reconcile_marker",
        lambda self, **_kwargs: None,
    )
    # Bypass the zero-progress pause so the debounce sleeps dominate.
    monkeypatch.setenv(
        "VECTOR_LAKE_WIKI_RECONCILE_ZERO_PROGRESS_POLL_SECONDS",
        "0.05",
    )

    watchdog_app.index_worker_loop(_SleepThenStop([0.15, 0.0]))
    return len(refresh_calls)


def test_topology_staleness_cap_is_armed_without_projection_progress(
    isolated_memory,
    monkeypatch,
):
    # The cap was previously armed only when a projection batch completed, so a
    # permanently dirty graph could never be refreshed.
    assert _drive_topology_cap(monkeypatch, isolated_memory, with_manifest=True) == 1


def test_topology_cap_not_armed_without_a_published_pair(
    isolated_memory,
    monkeypatch,
):
    assert _drive_topology_cap(monkeypatch, isolated_memory, with_manifest=False) == 0
