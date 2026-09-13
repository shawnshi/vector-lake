from __future__ import annotations

import json

import pytest

from vector_lake import watchdog_status
from vector_lake import db_store
from vector_lake import ingest_worker


def test_idle_component_heartbeats_are_coalesced_but_transitions_publish(
    isolated_memory, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(watchdog_status, "_monotonic_now", lambda: clock[0])
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_STATUS_HEARTBEAT_SECONDS", "30")
    real_publish = watchdog_status._publish_locked
    publishes = []

    def counted_publish(path, data):
        publishes.append(data["status"])
        return real_publish(path, data)

    monkeypatch.setattr(watchdog_status, "_publish_locked", counted_publish)
    watchdog_status.begin_watchdog_run(("watchdog", "outbox"))
    baseline = len(publishes)

    assert watchdog_status.write_status("idle", 0, 0, "idle", component="outbox")
    assert len(publishes) == baseline + 1
    for _ in range(100):
        assert watchdog_status.write_status(
            "idle", 0, 0, "idle", component="outbox"
        )
    assert len(publishes) == baseline + 1

    clock[0] += 31
    assert watchdog_status.write_status("idle", 0, 0, "idle", component="outbox")
    assert len(publishes) == baseline + 2
    assert watchdog_status.write_status(
        "error", 0, 0, "failed", "boom", component="outbox"
    )
    assert len(publishes) == baseline + 3


def test_five_minute_idle_publish_count_is_bounded_and_heartbeats_stay_fresh(
    isolated_memory, monkeypatch
):
    components = ("watchdog", "outbox", "scheduler", "ingest", "auto_ingest")
    clock = [0.0]
    monkeypatch.setattr(watchdog_status, "_monotonic_now", lambda: clock[0])
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_STATUS_HEARTBEAT_SECONDS", "30")
    real_publish = watchdog_status._publish_locked
    publishes = 0

    def counted_publish(path, data):
        nonlocal publishes
        publishes += 1
        return real_publish(path, data)

    monkeypatch.setattr(watchdog_status, "_publish_locked", counted_publish)
    watchdog_status.begin_watchdog_run(components)
    for component in components:
        state = "disabled" if component == "auto_ingest" else "idle"
        assert watchdog_status.write_status(
            state, 0, 0, f"{component} steady", component=component
        )
    for second in range(1, 301):
        clock[0] = float(second)
        for component in components:
            state = "disabled" if component == "auto_ingest" else "idle"
            assert watchdog_status.write_status(
                state, 0, 0, f"{component} steady", component=component
            )

    status = json.loads(
        watchdog_status.get_status_file().read_text(encoding="utf-8")
    )
    assert publishes <= 75
    assert set(status["components"]) == set(components)
    assert all(item["heartbeat_at"] for item in status["components"].values())


def test_idle_ingest_poll_does_not_publish_a_fake_processing_transition(
    monkeypatch,
):
    published = []

    class StopAfterFirstWait:
        def is_set(self):
            return False

        def wait(self, _seconds):
            return True

    monkeypatch.setattr(ingest_worker, "process_jobs", lambda: 0)
    monkeypatch.setattr(
        ingest_worker,
        "write_status",
        lambda state, *_args, **kwargs: published.append(
            (state, kwargs.get("component"))
        ),
    )

    ingest_worker.start_worker(StopAfterFirstWait())

    assert published == [
        ("idle", "ingest"),
        ("idle", "ingest"),
        ("stopped", "ingest"),
    ]


def test_schema_maintenance_window_defers_the_ingest_dispatcher(monkeypatch):
    """An operator schema migration must defer the dispatcher, not break it.

    Regression: ``init_db`` raised a bare RuntimeError, so every worker loop
    logged ``Worker exception: Database schema migration maintenance window is
    active`` and flipped the component to error (13 occurrences in one
    afternoon).  The typed exception carries the lock's retry delay instead.
    """
    published = []
    retried = []

    class StopAfterFirstWait:
        def is_set(self):
            return False

        def wait(self, seconds):
            retried.append(seconds)
            return True

    def raise_maintenance_window():
        raise db_store.SchemaMaintenanceActive(
            "Database schema migration maintenance window is active",
            retry_after_seconds=5.0,
        )

    monkeypatch.setattr(ingest_worker, "process_jobs", raise_maintenance_window)
    monkeypatch.setattr(
        ingest_worker,
        "write_status",
        lambda state, *_args, **kwargs: published.append(
            (state, kwargs.get("component"))
        ),
    )

    ingest_worker.start_worker(StopAfterFirstWait())

    assert published == [
        ("idle", "ingest"),
        ("idle", "ingest"),
        ("stopped", "ingest"),
    ]
    # Deferral uses the reported delay, not the 15s generic error backoff.
    assert retried == [5.0]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 3.0),
        ("0", 0.0),
        ("7.5", 7.5),
        ("600", 30.0),
        ("invalid", 3.0),
    ],
)
def test_watchdog_gate_wait_is_bounded_and_configurable(monkeypatch, configured, expected):
    """Watchdog maintenance must take a bounded turn on the shared gate.

    Every watchdog admission previously used ``wait_timeout_seconds=0``, which can
    never win the gate while an MCP tool or an operator GC holds it.
    """
    from vector_lake import watchdog_app

    if configured is None:
        monkeypatch.delenv("VECTOR_LAKE_WATCHDOG_GATE_WAIT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_GATE_WAIT_SECONDS", configured)

    assert watchdog_app._watchdog_gate_wait_seconds() == expected
