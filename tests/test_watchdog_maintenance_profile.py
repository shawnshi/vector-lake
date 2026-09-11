import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest

import watchdog_sync
from vector_lake.runtime_health import _watchdog_component_health
from vector_lake.watchdog_status import begin_watchdog_run, get_status_file, write_status


def test_entrypoint_dispatches_explicit_maintenance(monkeypatch):
    from vector_lake import watchdog_app

    observed = []
    monkeypatch.setattr(watchdog_sync, "_bootstrap_runtime_paths", lambda: {})
    monkeypatch.setattr(
        watchdog_app,
        "start_watchdog",
        lambda **kwargs: observed.append(kwargs),
    )

    assert watchdog_sync.main(["--maintenance"]) == 0
    assert observed == [{"maintenance": True}]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--maintenance", "--stop"],
        ["--maintenance", "extra"],
        ["--unknown"],
    ],
)
def test_entrypoint_rejects_invalid_maintenance_combinations(arguments):
    with pytest.raises(SystemExit, match="usage"):
        watchdog_sync.main(arguments)


def test_maintenance_lifecycle_starts_only_real_outbox_owner(
    isolated_memory, monkeypatch
):
    from vector_lake import auto_ingest_worker, db_store, ingest_worker, watchdog_app

    def forbidden(*_args, **_kwargs):
        raise AssertionError("full-profile constructor called in maintenance")

    monkeypatch.setattr(watchdog_app, "Observer", forbidden)
    monkeypatch.setattr(watchdog_app, "_watch_directories", forbidden)
    monkeypatch.setattr(watchdog_app, "index_worker_loop", forbidden)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", forbidden)
    monkeypatch.setattr(ingest_worker, "start_worker", forbidden)
    monkeypatch.setattr(auto_ingest_worker, "start_auto_ingest_worker", forbidden)
    monkeypatch.setattr(watchdog_app, "_check_projection_store_for_startup", lambda: None)
    monkeypatch.setattr(db_store, "mutation_outbox_has_claimable", lambda: False)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_SHUTDOWN_TIMEOUT_SECONDS", "1")

    stop_event = threading.Event()
    errors = []
    runner = threading.Thread(
        target=lambda: _capture_error(
            errors, watchdog_app._start_watchdog_locked, stop_event, maintenance=True
        )
    )
    runner.start()
    try:
        deadline = time.monotonic() + 2
        status = {}
        while time.monotonic() < deadline:
            try:
                status = json.loads(get_status_file().read_text(encoding="utf-8"))
            except (FileNotFoundError, PermissionError):
                # A Windows atomic replacement can transiently deny a reader.
                time.sleep(0.01)
                continue
            if status["components"]["outbox"]["status"] == "idle":
                break
            time.sleep(0.01)
        assert status["components"]["outbox"]["status"] == "idle"
        assert status["components"]["outbox"]["run_id"] == status["run_id"]
        assert status["components"]["outbox"]["process_id"] == os.getpid()
    finally:
        # Finish before monkeypatch restores real runtime paths, even on failure.
        stop_event.set()
        runner.join(3)
    assert not runner.is_alive()
    assert errors == []
    final = json.loads(get_status_file().read_text(encoding="utf-8"))
    assert final["components"]["outbox"]["status"] == "stopped"


def test_maintenance_outbox_defers_to_foreground_heavy_gate(isolated_memory, monkeypatch):
    from vector_lake import db_store, watchdog_app
    from vector_lake.heavy_task_gate import heavy_task

    db_store.init_db()
    stop = threading.Event()
    processed = []
    deferred = []
    errors = []

    def publish(state, *_args, **_kwargs):
        if "Maintenance outbox deferred" in str(_args):
            deferred.append(state)
            stop.set()
        return True

    def process(**_kwargs):
        processed.append(True)
        stop.set()
        return {"claimed": 0, "completed": 0, "retrying": 0, "failed": 0}

    monkeypatch.setattr(db_store, "mutation_outbox_has_claimable", lambda: True)
    monkeypatch.setattr(watchdog_app, "process_mutation_outbox_batch", process)
    monkeypatch.setattr(watchdog_app, "write_status", publish)
    runner = threading.Thread(
        target=lambda: _capture_error(errors, watchdog_app.maintenance_outbox_worker_loop, stop)
    )
    try:
        with heavy_task("maintenance", "test-foreground-write", origin="test", wait_timeout_seconds=0):
            runner.start()
            runner.join(3)
            assert not runner.is_alive()
            assert processed == []
            assert deferred == ["idle"]
            assert errors == []
    finally:
        stop.set()
        runner.join(3)


def _capture_error(errors, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except BaseException as exc:
        errors.append(exc)


def test_maintenance_profile_is_run_fenced_and_exact(isolated_memory):
    run_id = begin_watchdog_run(
        ("watchdog", "outbox"), run_profile="maintenance"
    )
    status = json.loads(get_status_file().read_text(encoding="utf-8"))
    assert status["schema_version"] == 4
    assert status["run_profile"] == {
        "schema_version": 1,
        "name": "maintenance",
        "components": ["watchdog", "outbox"],
    }
    assert status["run_id"] == run_id
    assert all(
        item["run_id"] == run_id and item["process_id"] == os.getpid()
        for item in status["components"].values()
    )


def test_legacy_direct_status_does_not_declare_empty_full_profile(isolated_memory):
    assert write_status("idle", 0, 0, "legacy heartbeat", component="watchdog")
    status = json.loads(get_status_file().read_text(encoding="utf-8"))

    assert status["schema_version"] == 3
    assert "run_profile" not in status


@pytest.mark.parametrize("state", [None, "stopped", "idle"])
def test_maintenance_missing_stopped_or_stale_outbox_blocks(state):
    now = datetime.now(timezone.utc)
    status = _maintenance_status(now)
    if state is None:
        status["components"].pop("outbox")
    else:
        status["components"]["outbox"]["status"] = state
        if state == "idle":
            status["components"]["outbox"]["heartbeat_at"] = "2000-01-01T00:00:00+00:00"

    health = _watchdog_component_health(
        status, now_utc=now, component_max_age=120, auto_ingest_enabled=False
    )

    assert (
        health["missing_components_blocking"]
        or health["unhealthy_required_components"]
        or health["stale_required_components"]
    )


@pytest.mark.parametrize("extra", ["ingest", "scheduler", "auto_ingest"])
def test_maintenance_rejects_extraneous_actual_component(extra):
    now = datetime.now(timezone.utc)
    status = _maintenance_status(now)
    status["components"][extra] = dict(status["components"]["watchdog"])

    health = _watchdog_component_health(
        status, now_utc=now, component_max_age=120, auto_ingest_enabled=False
    )

    assert "watchdog_run_profile_components_mismatch" in health["profile_issues"]
    assert health["aggregate_requires_block"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda status: status.update(schema_version=5),
        lambda status: status.update(run_id=""),
        lambda status: status.update(process_id=0),
        lambda status: status["run_profile"].update(schema_version=2),
    ],
)
def test_malformed_profile_or_owner_fails_closed(mutation):
    now = datetime.now(timezone.utc)
    status = _maintenance_status(now)
    mutation(status)
    health = _watchdog_component_health(
        status, now_utc=now, component_max_age=120, auto_ingest_enabled=False
    )
    assert health["profile_issues"]
    assert health["aggregate_requires_block"] is True


@pytest.mark.parametrize("component", ["watchdog", "outbox"])
@pytest.mark.parametrize("state", ["starting", "disabled", "", "unknown", "paused"])
def test_maintenance_nonoperational_states_block_full_health(
    isolated_memory, component, state
):
    from vector_lake import db_store
    from vector_lake.runtime_health import assess_runtime_health

    db_store.init_db()
    begin_watchdog_run(("watchdog", "outbox"), run_profile="maintenance")
    for name in ("watchdog", "outbox"):
        assert write_status("idle", 0, 0, component=name)
    assert write_status(state, 0, 0, component=component)
    health = assess_runtime_health(deep_projection_checks=False)
    assert health["ok"] is False
    assert f"watchdog_unhealthy:{component}" in health["issues"]


@pytest.mark.parametrize("outbox_state", ["idle", "processing"])
def test_maintenance_operational_states_pass_full_health(isolated_memory, outbox_state):
    from vector_lake import db_store
    from vector_lake.runtime_health import assess_runtime_health

    db_store.init_db()
    begin_watchdog_run(("watchdog", "outbox"), run_profile="maintenance")
    assert write_status("idle", 0, 0, component="watchdog")
    assert write_status(outbox_state, 0, 0, component="outbox")
    assert assess_runtime_health(deep_projection_checks=False)["ok"] is True


def _maintenance_status(now):
    return {
        "schema_version": 4,
        "run_id": "run-1",
        "process_id": os.getpid(),
        "expected_components": ["watchdog", "outbox"],
        "run_profile": {
            "schema_version": 1,
            "name": "maintenance",
            "components": ["watchdog", "outbox"],
        },
        "components": {
            name: {
                "status": "idle",
                "heartbeat_at": now.isoformat(),
                "run_id": "run-1",
                "process_id": os.getpid(),
            }
            for name in ("watchdog", "outbox")
        },
        "status": "idle",
    }


def test_maintenance_profile_ignores_global_auto_ingest_requirement(monkeypatch):
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    components = {
        name: {
            "status": "idle",
            "heartbeat_at": timestamp,
            "run_id": "run-1",
            "process_id": os.getpid(),
        }
        for name in ("watchdog", "outbox")
    }
    status = {
        "schema_version": 4,
        "run_id": "run-1",
        "process_id": os.getpid(),
        "expected_components": ["watchdog", "outbox"],
        "run_profile": {
            "schema_version": 1,
            "name": "maintenance",
            "components": ["watchdog", "outbox"],
        },
        "components": components,
        "status": "idle",
    }
    monkeypatch.setenv(
        "VECTOR_LAKE_WATCHDOG_REQUIRED_COMPONENTS",
        "watchdog,outbox,ingest,auto_ingest",
    )

    health = _watchdog_component_health(
        status,
        now_utc=now,
        component_max_age=120,
        auto_ingest_enabled=True,
    )

    assert health["required_components"] == ["outbox", "watchdog"]
    assert health["missing_components"] == []
    assert health["profile_issues"] == []


def test_full_profile_keeps_scheduler_optional(monkeypatch):
    now = datetime.now(timezone.utc)
    inventory = ["watchdog", "outbox", "scheduler", "ingest", "auto_ingest"]
    status = {
        "schema_version": 4,
        "run_id": "run-1",
        "process_id": os.getpid(),
        "expected_components": inventory,
        "run_profile": {
            "schema_version": 1,
            "name": "full",
            "components": inventory,
        },
        "components": {
            name: {
                "status": "error" if name == "scheduler" else "idle",
                "heartbeat_at": now.isoformat(),
                "run_id": "run-1",
                "process_id": os.getpid(),
            }
            for name in inventory
        },
        "status": "error",
    }
    monkeypatch.setenv(
        "VECTOR_LAKE_WATCHDOG_REQUIRED_COMPONENTS", "watchdog,outbox,ingest"
    )

    health = _watchdog_component_health(
        status, now_utc=now, component_max_age=120, auto_ingest_enabled=False
    )

    assert "scheduler" not in health["required_components"]
    assert health["unhealthy_optional_components"] == ["scheduler"]
    assert health["aggregate_requires_block"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda status: status["run_profile"].update(name="unknown"),
        lambda status: status["run_profile"]["components"].append("ingest"),
        lambda status: status["components"]["outbox"].update(run_id="foreign"),
    ],
)
def test_invalid_or_inconsistent_profile_fails_closed(mutation):
    now = datetime.now(timezone.utc)
    status = {
        "schema_version": 4,
        "run_id": "run-1",
        "process_id": os.getpid(),
        "expected_components": ["watchdog", "outbox"],
        "run_profile": {
            "schema_version": 1,
            "name": "maintenance",
            "components": ["watchdog", "outbox"],
        },
        "components": {
            name: {
                "status": "idle",
                "heartbeat_at": now.isoformat(),
                "run_id": "run-1",
                "process_id": os.getpid(),
            }
            for name in ("watchdog", "outbox")
        },
        "status": "idle",
    }
    mutation(status)

    health = _watchdog_component_health(
        status,
        now_utc=now,
        component_max_age=120,
        auto_ingest_enabled=False,
    )

    assert health["profile_issues"]
    assert health["aggregate_requires_block"] is True
