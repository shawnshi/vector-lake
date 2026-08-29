from __future__ import annotations

import json

from vector_lake import watchdog_status
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
