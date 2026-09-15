"""Breaker cooldown expiry must restore the configured failure threshold."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vector_lake import auto_ingest_worker
from vector_lake.auto_ingest_worker import (
    _global_budget_block,
    _load_state,
    _record_infrastructure_failure,
    _reconcile_expired_circuit,
)
from vector_lake.tool_auto_ingest import auto_ingest_budget_status


def _now() -> datetime:
    return datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def _save(
    *,
    failures: int,
    circuit_open_until: datetime | None,
) -> None:
    state = auto_ingest_worker._empty_state()
    state["consecutive_infra_failures"] = failures
    state["circuit_open_until"] = (
        circuit_open_until.isoformat() if circuit_open_until is not None else None
    )
    auto_ingest_worker._save_state(state)


def test_expired_circuit_window_closes_the_breaker_and_clears_the_streak(
    isolated_memory,
):
    # The live regression: the threshold was 3 but the persisted streak had
    # reached 10 with an already-elapsed window, so every later failure
    # re-opened a full cooldown and the threshold effectively became 1.
    _save(failures=10, circuit_open_until=_now() - timedelta(hours=4))
    state = _load_state(_now())

    assert _reconcile_expired_circuit(state, _now()) is True
    assert state["consecutive_infra_failures"] == 0
    assert state["circuit_open_until"] is None

    persisted = _load_state(_now())
    assert persisted["consecutive_infra_failures"] == 0
    assert persisted["circuit_open_until"] is None


def test_active_circuit_window_is_left_untouched(isolated_memory):
    open_until = _now() + timedelta(minutes=30)
    _save(failures=3, circuit_open_until=open_until)
    state = _load_state(_now())

    assert _reconcile_expired_circuit(state, _now()) is False
    assert state["consecutive_infra_failures"] == 3
    assert state["circuit_open_until"] == open_until.isoformat()


def test_closed_and_unset_breakers_are_not_rewritten(isolated_memory):
    _save(failures=0, circuit_open_until=None)
    state = _load_state(_now())

    assert _reconcile_expired_circuit(state, _now()) is False
    assert state["consecutive_infra_failures"] == 0


def test_threshold_needs_its_full_count_again_after_the_window_elapses(
    isolated_memory,
):
    config = auto_ingest_worker.AutoIngestConfig()
    assert config.max_consecutive_infra_failures == 3

    _save(failures=10, circuit_open_until=_now() - timedelta(minutes=1))
    state = _load_state(_now())
    _reconcile_expired_circuit(state, _now())

    # One failure after recovery is no longer enough to re-open the breaker.
    now = _now()
    _record_infrastructure_failure(state, config, now)
    assert state["consecutive_infra_failures"] == 1
    assert state["circuit_open_until"] is None
    assert _global_budget_block(config, state, now) == ""

    _record_infrastructure_failure(state, config, now)
    assert state["circuit_open_until"] is None

    _record_infrastructure_failure(state, config, now)
    assert state["consecutive_infra_failures"] == 3
    assert state["circuit_open_until"] is not None
    blocked = _global_budget_block(config, state, now)
    assert blocked.startswith("circuit_open_until:")


def test_budget_status_publishes_the_threshold_it_is_compared_against(
    isolated_memory,
):
    _save(failures=10, circuit_open_until=_now() - timedelta(hours=1))

    report = auto_ingest_budget_status(now=_now())

    assert report["limits"]["max_consecutive_infra_failures"] == 3
    assert report["limits"]["circuit_breaker_seconds"] == 3600
    assert report["circuit"]["consecutive_infra_failures"] == 10
    assert report["circuit"]["is_open"] is False


def test_disabled_host_still_reconciles_an_expired_breaker(isolated_memory):
    """The disabled tick returns before the launch path, so it must clear the
    breaker itself.

    Runtime Health reads ``consecutive_infra_failures`` whether or not the
    execution host is enabled, so a streak left above the threshold by a host
    that is switched off would be reported forever.
    """
    import json
    import threading

    from vector_lake import auto_ingest_worker
    from vector_lake.auto_ingest_worker import AutoIngestController

    auto_ingest_worker._config_path().write_text(
        json.dumps({"schema_version": 1, "enabled": False}), encoding="utf-8"
    )
    _save(failures=10, circuit_open_until=_now() - timedelta(hours=4))

    outcome = AutoIngestController().tick(threading.Event())

    assert outcome == "disabled"
    persisted = _load_state(_now())
    assert persisted["consecutive_infra_failures"] == 0
    assert persisted["circuit_open_until"] is None
