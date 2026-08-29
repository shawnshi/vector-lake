import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest

from vector_lake import db_store
from vector_lake.runtime_health import assess_runtime_health
from vector_lake.watchdog_status import (
    begin_watchdog_run,
    current_watchdog_run_id,
    get_status_file,
    write_status,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows process probe regression")
def test_process_is_alive_uses_windows_process_handle_for_foreign_pid():
    from vector_lake.watchdog_status import _process_is_alive

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert _process_is_alive(process.pid) is True
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert _process_is_alive(process.pid) is False


def test_watchdog_run_generation_atomically_replaces_foreign_components(
    isolated_memory,
):
    expected = ("watchdog", "outbox", "scheduler", "ingest", "auto_ingest")
    run_id = begin_watchdog_run(expected)
    status_path = get_status_file()
    status = json.loads(status_path.read_text(encoding="utf-8"))

    assert run_id == current_watchdog_run_id() == status["run_id"]
    assert status["schema_version"] == 3
    assert status["process_id"] == os.getpid()
    assert status["expected_components"] == list(expected)
    assert set(status["components"]) == set(expected)
    assert {
        component["run_id"] for component in status["components"].values()
    } == {run_id}
    assert {
        component["status"] for component in status["components"].values()
    } == {"starting"}

    foreign = dict(status)
    foreign["run_id"] = "foreign-generation"
    foreign["process_id"] = 999999
    status_path.write_text(json.dumps(foreign), encoding="utf-8")
    assert write_status("idle", 0, 0, component="watchdog") is False
    assert json.loads(status_path.read_text(encoding="utf-8"))["run_id"] == (
        "foreign-generation"
    )

    replacement = begin_watchdog_run(("watchdog", "outbox"))
    replaced = json.loads(status_path.read_text(encoding="utf-8"))
    assert replacement != run_id
    assert replaced["run_id"] == replacement
    assert set(replaced["components"]) == {"watchdog", "outbox"}

    replaced["components"]["foreign"] = {
        "run_id": "foreign-generation",
        "process_id": 999999,
        "status": "idle",
    }
    status_path.write_text(json.dumps(replaced), encoding="utf-8")
    assert write_status("idle", 0, 0, component="watchdog") is True
    cleaned = json.loads(status_path.read_text(encoding="utf-8"))
    assert set(cleaned["components"]) == {"watchdog", "outbox"}


def test_watchdog_run_refuses_a_live_foreign_generation(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import watchdog_status

    status_path = get_status_file()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "run_id": "still-draining",
                "process_id": 424242,
                "components": {
                    "auto_ingest": {"status": "draining"},
                    "watchdog": {"status": "halted"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(watchdog_status, "_process_is_alive", lambda _pid: True)

    with pytest.raises(RuntimeError, match="prior status owner is still alive"):
        begin_watchdog_run(("watchdog", "auto_ingest"))

    preserved = json.loads(status_path.read_text(encoding="utf-8"))
    assert preserved["run_id"] == "still-draining"


def test_disabled_auto_ingest_is_explicit_without_overriding_idle_aggregate(
    isolated_memory,
):
    components = ("watchdog", "outbox", "scheduler", "ingest", "auto_ingest")
    begin_watchdog_run(components)

    for component in components[:-1]:
        assert write_status("idle", 0, 0, component=component) is True
    assert (
        write_status(
            "disabled",
            0,
            0,
            "Automatic ingest host disabled",
            component="auto_ingest",
        )
        is True
    )

    status = json.loads(get_status_file().read_text(encoding="utf-8"))
    assert status["status"] == "idle"
    assert status["components"]["auto_ingest"]["status"] == "disabled"


def test_component_heartbeat_staleness_is_not_hidden_by_fresh_top_level(
    isolated_memory,
):
    db_store.init_db()
    for component in ("outbox", "scheduler", "ingest", "watchdog"):
        write_status(
            "idle",
            0,
            0,
            f"{component} heartbeat",
            "",
            component=component,
        )

    from vector_lake import runtime_health

    fresh_token = runtime_health._write_health_surface_token()
    status_path = get_status_file()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    status["components"]["outbox"]["heartbeat_at"] = "2000-01-01T00:00:00Z"
    status["components"]["outbox"]["updated_at"] = "2000-01-01T00:00:00Z"
    status_path.write_text(json.dumps(status), encoding="utf-8")

    stale_token = runtime_health._write_health_surface_token()
    health = assess_runtime_health(max_watchdog_age_seconds=120)

    assert stale_token != fresh_token
    assert health["detail"]["watchdog_age_seconds"] <= 2
    assert any(
        issue.startswith("watchdog_component_stale:outbox:")
        for issue in health["issues"]
    )


def test_legacy_watchdog_status_remains_readable(isolated_memory):
    db_store.init_db()
    status_path = get_status_file()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "status": "idle",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "current_action": "legacy heartbeat",
            }
        ),
        encoding="utf-8",
    )

    health = assess_runtime_health()

    assert health["detail"]["watchdog_status_schema"] == "legacy"
    assert "watchdog_component_status_legacy" in health["warnings"]
    assert not any(
        issue.startswith("watchdog_component_stale:") for issue in health["issues"]
    )


def test_runtime_health_does_not_create_default_meta_directory(isolated_memory):
    meta_dir = isolated_memory / "wiki" / ".meta"
    assert not meta_dir.exists()

    health = assess_runtime_health()

    assert health["status"] == "blocked"
    assert "database_missing" in health["issues"][0]
    assert not meta_dir.exists()


def test_runtime_health_does_not_create_missing_database(tmp_path, monkeypatch):
    db_path = tmp_path / "missing" / "vector_lake.db"
    db_store.close_connection()
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(db_path))

    health = assess_runtime_health()

    assert health["ok"] is False
    assert health["status"] == "blocked"
    assert health["detail"]["database_access"] == "read_only"
    assert "database_missing" in health["issues"][0]
    assert not db_path.exists()


def test_explicit_write_gate_can_bootstrap_storage(isolated_memory):
    from vector_lake.runtime_health import enforce_runtime_write_health

    db_path = db_store.get_db_path()
    assert not db_path.exists()

    enforce_runtime_write_health()

    assert db_path.exists()


def test_runtime_health_reports_unmigrated_schema_without_mutating_it(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY, data_json TEXT)")
    conn.commit()
    conn.close()
    db_store.close_connection()
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(db_path))
    before = db_path.read_bytes()

    health = assess_runtime_health()

    assert health["ok"] is False
    assert health["status"] == "blocked"
    assert "schema_not_ready" in health["issues"][0]
    assert db_path.read_bytes() == before
    check = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        check.close()
    assert tables == {"entities"}


def test_watchdog_detects_dead_worker_and_joins_remaining_workers(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, *_args, **_kwargs):
            return None

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    def stopped_worker(_stop_event):
        return None

    def cooperative_worker(stop_event):
        stop_event.wait(2)

    missing = isolated_memory / "not-watched"
    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda *_args: {"wiki": missing, "diary": missing, "raw": missing},
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", stopped_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_SHUTDOWN_TIMEOUT_SECONDS", "1")

    watchdog_app._start_watchdog_locked(threading.Event())

    status = json.loads(get_status_file().read_text(encoding="utf-8"))
    assert status["components"]["outbox"]["status"] == "halted"
    assert status["components"]["watchdog"]["status"] == "halted"
    assert watchdog_app.background_thread_health() == {
        "outbox": False,
        "scheduler": False,
        "ingest": False,
        "auto_ingest": False,
    }


def test_watchdog_stops_emitters_after_partial_observer_start_failure(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    missing = isolated_memory / "not-watched"
    state = {
        "emitter_alive": False,
        "stop_calls": 0,
        "join_calls": 0,
        "raw_shutdown": 0,
    }

    class FakeObserver:
        def schedule(self, *_args, **_kwargs):
            return object()

        def start(self):
            state["emitter_alive"] = True
            raise RuntimeError("injected partial observer start failure")

        def stop(self):
            state["stop_calls"] += 1
            state["emitter_alive"] = False

        def join(self, timeout=None):
            state["join_calls"] += 1

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            raise AssertionError("startup scan ran after observer start failure")

        def shutdown(self, timeout_seconds=None):
            state["raw_shutdown"] += 1
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_raw_watch_configuration",
        lambda: ([raw_dir.resolve()], "{}"),
    )
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)

    with pytest.raises(
        RuntimeError,
        match="injected partial observer start failure",
    ):
        watchdog_app._start_watchdog_locked(stop_event)

    assert state == {
        "emitter_alive": False,
        "stop_calls": 1,
        "join_calls": 0,
        "raw_shutdown": 1,
    }


def test_watchdog_start_requests_raw_recovery_scan(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    requested = threading.Event()
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    missing = isolated_memory / "not-watched"

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, *_args, **_kwargs):
            return None

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            requested.set()
            self.stop_event.set()

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda *_args: {"wiki": missing, "diary": missing, "raw": raw_dir},
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)

    watchdog_app._start_watchdog_locked(stop_event)

    assert requested.is_set()


def test_watchdog_schedules_all_collapsed_ingest_roots(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, tool_ingest, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    external = isolated_memory / "external-policy"
    nested = external / "nested"
    nested.mkdir(parents=True)
    missing = isolated_memory / "not-watched"
    config_root = isolated_memory / "watch-config"
    config_root.mkdir()
    (config_root / "config.json").write_text(
        json.dumps(
            {
                "target_directories": [str(external), str(nested)],
                "supported_extensions": [".md", ".txt"],
            }
        ),
        encoding="utf-8",
    )
    scheduled = []

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, handler, path, recursive=False):
            scheduled.append((handler, str(Path(path).resolve()), recursive))

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            self.stop_event.set()

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(tool_ingest, "get_extension_root", lambda: config_root)
    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda *_args: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": tool_ingest.get_ingest_target_directories(
                collapse_nested=True
            ),
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)

    watchdog_app._start_watchdog_locked(stop_event)

    raw_schedules = [item for item in scheduled if item[2] is True]
    assert {item[1] for item in raw_schedules} == {
        str(raw_dir.resolve()),
        str(external.resolve()),
    }
    assert len({id(item[0]) for item in raw_schedules}) == 1


def test_watchdog_hot_reconciles_removed_and_recreated_raw_roots(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, tool_ingest, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    initial_external = isolated_memory / "external-initial"
    added_external = isolated_memory / "external-added"
    initial_external.mkdir()
    added_external.mkdir()
    missing = isolated_memory / "not-watched"
    config_root = isolated_memory / "hot-watch-config"
    config_root.mkdir()
    config_path = config_root / "config.json"

    def write_config(targets):
        config_path.write_text(
            json.dumps(
                {
                    "target_directories": [str(path) for path in targets],
                    "supported_extensions": [".md", ".txt"],
                }
            ),
            encoding="utf-8",
        )

    write_config([initial_external])
    scheduled = []
    unscheduled = []
    unschedule_failures = []
    scan_requests = []

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, handler, path, recursive=False):
            handle = (
                str(Path(path).resolve()),
                len(scheduled),
            )
            scheduled.append((handler, handle[0], recursive, handle))
            return handle

        def unschedule(self, handle):
            if not unschedule_failures:
                unschedule_failures.append(handle)
                raise OSError("injected unschedule interruption")
            unscheduled.append(handle)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            scan_requests.append(True)
            if len(scan_requests) == 1:
                write_config([initial_external, added_external])
            elif len(scan_requests) == 2:
                raise RuntimeError("injected full-scan submission failure")
            elif len(scan_requests) == 3:
                write_config([added_external])
            elif len(scan_requests) == 4:
                added_external.rmdir()
            elif len(scan_requests) == 5:
                added_external.mkdir()
            else:
                self.stop_event.set()

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(tool_ingest, "get_extension_root", lambda: config_root)
    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda *_args: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": tool_ingest.get_ingest_target_directories(
                collapse_nested=True
            ),
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS", "0.05")

    watchdog_app._start_watchdog_locked(stop_event)

    raw_schedules = [item for item in scheduled if item[2] is True]
    scheduled_paths = [item[1] for item in raw_schedules]
    raw_key = str(raw_dir.resolve())
    initial_key = str(initial_external.resolve())
    added_key = str(added_external.resolve())
    assert set(scheduled_paths) == {raw_key, initial_key, added_key}
    assert scheduled_paths.count(raw_key) == 1
    assert scheduled_paths.count(initial_key) == 1
    assert scheduled_paths.count(added_key) == 2
    assert {handle[0] for handle in unscheduled} == {
        initial_key,
        added_key,
    }
    assert {handle[0] for handle in unschedule_failures} == {initial_key}
    assert len(scan_requests) == 6


def test_watchdog_halts_when_non_raw_emitter_dies(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    wiki_dir = isolated_memory / "wiki"
    raw_dir = isolated_memory / "raw"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    missing = isolated_memory / "not-watched"
    state = {"stop_calls": 0, "join_calls": 0, "scan_requests": 0}

    class FakeEmitter:
        def __init__(self, watch):
            self.watch = watch
            self.alive = True

        def is_alive(self):
            return self.alive

    class FakeObserver:
        def __init__(self):
            self.alive = False
            self.emitters = []
            self.non_raw_emitter = None

        def schedule(self, _handler, path, recursive=False):
            watch = (str(Path(path).resolve()), recursive, len(self.emitters))
            emitter = FakeEmitter(watch)
            self.emitters.append(emitter)
            if not recursive:
                self.non_raw_emitter = emitter
            return watch

        def start(self):
            self.alive = True
            self.non_raw_emitter.alive = False

        def is_alive(self):
            return self.alive

        def stop(self):
            state["stop_calls"] += 1
            self.alive = False
            for emitter in self.emitters:
                emitter.alive = False

        def join(self, timeout=None):
            state["join_calls"] += 1

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            state["scan_requests"] += 1

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_raw_watch_configuration",
        lambda: ([raw_dir.resolve()], "stable"),
    )
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": wiki_dir,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_SHUTDOWN_TIMEOUT_SECONDS", "1")

    watchdog_app._start_watchdog_locked(stop_event)

    status = json.loads(get_status_file().read_text(encoding="utf-8"))
    assert status["components"]["watchdog"]["status"] == "halted"
    assert status["components"]["watchdog"]["last_error"] == (
        "filesystem observer emitter stopped unexpectedly"
    )
    assert state == {"stop_calls": 1, "join_calls": 1, "scan_requests": 1}


def test_watchdog_resubscribes_when_one_raw_emitter_dies(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    missing = isolated_memory / "not-watched"
    state = {
        "observer": None,
        "raw_schedules": [],
        "unscheduled": [],
        "scan_requests": 0,
    }

    class FakeEmitter:
        def __init__(self, watch):
            self.watch = watch
            self.alive = True

        def is_alive(self):
            return self.alive

    class FakeObserver:
        def __init__(self):
            self.alive = False
            self.emitters = []
            state["observer"] = self

        def schedule(self, _handler, path, recursive=False):
            resolved = str(Path(path).resolve())
            watch = (resolved, recursive, len(state["raw_schedules"]))
            self.emitters.append(FakeEmitter(watch))
            if recursive:
                state["raw_schedules"].append(watch)
            return watch

        def unschedule(self, watch):
            state["unscheduled"].append(watch)
            self.emitters = [
                emitter for emitter in self.emitters if emitter.watch != watch
            ]

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False
            for emitter in self.emitters:
                emitter.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            state["scan_requests"] += 1
            if state["scan_requests"] == 1:
                state["observer"].emitters[0].alive = False
            else:
                self.stop_event.set()

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_raw_watch_configuration",
        lambda: ([raw_dir.resolve()], "stable"),
    )
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS", "0.05")

    watchdog_app._start_watchdog_locked(stop_event)

    assert len(state["raw_schedules"]) == 2
    assert state["unscheduled"] == [state["raw_schedules"][0]]
    assert state["scan_requests"] == 2


def test_watchdog_retries_one_failed_raw_schedule_without_blocking_others(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    good_dir = isolated_memory / "external-good"
    flaky_dir = isolated_memory / "external-flaky"
    for directory in (raw_dir, good_dir, flaky_dir):
        directory.mkdir(parents=True, exist_ok=True)
    missing = isolated_memory / "not-watched"
    state = {
        "expanded": False,
        "attempts": {},
        "scheduled": [],
        "scan_requests": 0,
    }

    def raw_configuration():
        targets = [raw_dir.resolve()]
        token = "initial"
        if state["expanded"]:
            targets.extend([good_dir.resolve(), flaky_dir.resolve()])
            token = "expanded"
        return targets, token

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, _handler, path, recursive=False):
            resolved = str(Path(path).resolve())
            state["attempts"][resolved] = state["attempts"].get(resolved, 0) + 1
            if (
                resolved == str(flaky_dir.resolve())
                and state["attempts"][resolved] == 1
            ):
                raise OSError("injected per-target schedule failure")
            handle = (resolved, recursive, len(state["scheduled"]))
            state["scheduled"].append(handle)
            return handle

        def unschedule(self, _handle):
            return None

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            state["scan_requests"] += 1
            if state["scan_requests"] == 1:
                state["expanded"] = True
            elif state["scan_requests"] == 3:
                self.stop_event.set()

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_raw_watch_configuration",
        raw_configuration,
    )
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS", "0.05")

    watchdog_app._start_watchdog_locked(stop_event)

    raw_key = str(raw_dir.resolve())
    good_key = str(good_dir.resolve())
    flaky_key = str(flaky_dir.resolve())
    assert state["attempts"] == {raw_key: 1, good_key: 1, flaky_key: 2}
    assert [item[0] for item in state["scheduled"]] == [
        raw_key,
        good_key,
        flaky_key,
    ]
    assert state["scan_requests"] == 3


def test_watchdog_initial_raw_schedule_failure_isolated_and_retried(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    good_dir = isolated_memory / "initial-good"
    flaky_dir = isolated_memory / "initial-flaky"
    for directory in (raw_dir, good_dir, flaky_dir):
        directory.mkdir(parents=True, exist_ok=True)
    missing = isolated_memory / "not-watched"
    state = {
        "attempts": {},
        "scheduled": [],
        "scan_requests": 0,
    }

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, _handler, path, recursive=False):
            resolved = str(Path(path).resolve())
            state["attempts"][resolved] = state["attempts"].get(resolved, 0) + 1
            if (
                resolved == str(flaky_dir.resolve())
                and state["attempts"][resolved] == 1
            ):
                raise OSError("injected initial schedule failure")
            handle = (resolved, recursive, len(state["scheduled"]))
            state["scheduled"].append(handle)
            return handle

        def unschedule(self, _handle):
            return None

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            state["scan_requests"] += 1
            if state["scan_requests"] == 2:
                self.stop_event.set()

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_raw_watch_configuration",
        lambda: (
            [raw_dir.resolve(), good_dir.resolve(), flaky_dir.resolve()],
            "stable",
        ),
    )
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS", "0.05")

    watchdog_app._start_watchdog_locked(stop_event)

    raw_key = str(raw_dir.resolve())
    good_key = str(good_dir.resolve())
    flaky_key = str(flaky_dir.resolve())
    assert state["attempts"] == {raw_key: 1, good_key: 1, flaky_key: 2}
    assert [item[0] for item in state["scheduled"]] == [
        raw_key,
        good_key,
        flaky_key,
    ]
    assert state["scan_requests"] == 2


def test_watchdog_persistently_failed_raw_schedule_uses_bounded_backoff(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    flaky_dir = isolated_memory / "persistent-flaky"
    raw_dir.mkdir(parents=True, exist_ok=True)
    flaky_dir.mkdir()
    missing = isolated_memory / "not-watched"
    state = {
        "attempts": {},
        "observer_checks": 0,
        "scan_requests": 0,
    }

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, _handler, path, recursive=False):
            resolved = str(Path(path).resolve())
            state["attempts"][resolved] = state["attempts"].get(resolved, 0) + 1
            if resolved == str(flaky_dir.resolve()):
                raise OSError("persistent schedule failure")
            return (resolved, recursive)

        def unschedule(self, _handle):
            return None

        def start(self):
            self.alive = True

        def is_alive(self):
            state["observer_checks"] += 1
            if state["observer_checks"] >= 9:
                stop_event.set()
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            state["scan_requests"] += 1

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_raw_watch_configuration",
        lambda: ([raw_dir.resolve(), flaky_dir.resolve()], "stable"),
    )
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_RETRY_MAX_SECONDS", "0.2")

    watchdog_app._start_watchdog_locked(stop_event)

    flaky_attempts = state["attempts"][str(flaky_dir.resolve())]
    assert state["observer_checks"] >= 9
    assert 3 <= flaky_attempts <= 4
    assert state["scan_requests"] <= 4
    assert state["scan_requests"] < state["observer_checks"]


def test_watchdog_persistently_failed_raw_unschedule_uses_bounded_backoff(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    stale_dir = isolated_memory / "persistent-stale"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stale_dir.mkdir()
    missing = isolated_memory / "not-watched"
    state = {
        "removed": False,
        "unschedule_attempts": 0,
        "observer_checks": 0,
        "scan_requests": 0,
    }

    def raw_configuration():
        if state["removed"]:
            return [raw_dir.resolve()], "removed"
        return [raw_dir.resolve(), stale_dir.resolve()], "initial"

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, _handler, path, recursive=False):
            return (str(Path(path).resolve()), recursive)

        def unschedule(self, handle):
            if handle[0] == str(stale_dir.resolve()):
                state["unschedule_attempts"] += 1
                raise OSError("persistent unschedule failure")

        def start(self):
            self.alive = True

        def is_alive(self):
            state["observer_checks"] += 1
            if state["observer_checks"] >= 9:
                stop_event.set()
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            state["scan_requests"] += 1
            if state["scan_requests"] == 1:
                state["removed"] = True

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_raw_watch_configuration",
        raw_configuration,
    )
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_RETRY_MAX_SECONDS", "0.2")

    watchdog_app._start_watchdog_locked(stop_event)

    assert state["observer_checks"] >= 9
    assert 3 <= state["unschedule_attempts"] <= 4
    assert state["scan_requests"] == state["unschedule_attempts"] + 1
    assert state["scan_requests"] < state["observer_checks"]


def test_watchdog_hot_reconciles_parent_child_root_switch(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import ingest_worker, tool_ingest, watchdog_app

    stop_event = threading.Event()
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    parent = isolated_memory / "external-parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    missing = isolated_memory / "not-watched"
    config_root = isolated_memory / "parent-child-watch-config"
    config_root.mkdir()
    config_path = config_root / "config.json"

    def write_config(target):
        config_path.write_text(
            json.dumps(
                {
                    "target_directories": [str(target)],
                    "supported_extensions": [".md"],
                }
            ),
            encoding="utf-8",
        )

    write_config(nested)
    scheduled = []
    unscheduled = []
    active = {}
    scan_requests = []

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, handler, path, recursive=False):
            resolved = str(Path(path).resolve())
            handle = (resolved, len(scheduled))
            scheduled.append((handler, resolved, recursive, handle))
            active[handle] = resolved
            return handle

        def unschedule(self, handle):
            unscheduled.append(handle)
            active.pop(handle)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeRawHandler:
        def __init__(self, *, stop_event):
            self.stop_event = stop_event

        def request_full_scan(self):
            scan_requests.append(True)
            if len(scan_requests) == 1:
                write_config(parent)
            elif len(scan_requests) == 2:
                write_config(nested)
            else:
                self.stop_event.set()

        def shutdown(self, timeout_seconds=None):
            return True

    def cooperative_worker(event):
        event.wait(2)

    monkeypatch.setattr(tool_ingest, "get_extension_root", lambda: config_root)
    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(watchdog_app, "RawWatchdogHandler", FakeRawHandler)
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda raw_targets: {
            "wiki": missing,
            "diary": missing,
            "raw": raw_dir,
            "raw_targets": raw_targets,
        },
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", cooperative_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", cooperative_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", cooperative_worker)
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS", "0.05")

    watchdog_app._start_watchdog_locked(stop_event)

    scheduled_paths = [item[1] for item in scheduled if item[2] is True]
    assert scheduled_paths.count(str(raw_dir.resolve())) == 1
    assert scheduled_paths.count(str(parent.resolve())) == 1
    assert scheduled_paths.count(str(nested.resolve())) == 2
    assert set(active.values()) == {str(raw_dir.resolve()), str(nested.resolve())}
    assert {handle[0] for handle in unscheduled} == {
        str(parent.resolve()),
        str(nested.resolve()),
    }
    assert len(scan_requests) == 3


def test_raw_startup_overflow_scans_and_hashes_inventory_once(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_ingest
    from vector_lake.watchdog_app import RawWatchdogHandler

    raw_dir = isolated_memory / "raw"
    nested_dir = raw_dir / "overlap"
    nested_dir.mkdir()
    for index in range(120):
        parent = nested_dir if index < 60 else raw_dir
        (parent / f"startup-{index:03d}.txt").write_text(
            f"startup payload {index}",
            encoding="utf-8",
        )
    config_root = isolated_memory / "test-extension"
    config_root.mkdir()
    (config_root / "config.json").write_text(
        json.dumps(
            {
                "target_directories": [str(raw_dir), str(nested_dir)],
                "exclude_paths": [],
                "supported_extensions": [".txt"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_ingest, "get_extension_root", lambda: config_root)
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "isolated test ingest instructions",
    )
    real_revision = tool_ingest.stable_raw_revision
    real_walk = tool_ingest.os.walk
    hashed = []
    walked_roots = []

    def recording_revision(filepath, *args, **kwargs):
        snapshot = real_revision(filepath, *args, **kwargs)
        hashed.append(str(snapshot.path))
        return snapshot

    def recording_walk(root, *args, **kwargs):
        walked_roots.append(str(Path(root).resolve()))
        return real_walk(root, *args, **kwargs)

    real_prepare = tool_ingest.prepare_ingest_batch
    calls = []

    def recording_prepare(*args, **kwargs):
        calls.append((args, dict(kwargs)))
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(tool_ingest, "stable_raw_revision", recording_revision)
    monkeypatch.setattr(tool_ingest.os, "walk", recording_walk)
    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", recording_prepare)
    handler = RawWatchdogHandler()
    idle = False
    try:
        handler.request_full_scan()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with handler.lock:
                idle = (
                    len(calls) >= 1
                    and handler.sync_future is None
                    and not handler.pending_overflow
                )
            if idle:
                break
            time.sleep(0.01)
    finally:
        clean_shutdown = handler.shutdown(timeout_seconds=2)

    assert idle is True
    assert clean_shutdown is True
    assert len(calls) == 1
    assert calls[0][1]["candidate_paths"] is None
    assert calls[0][1]["_enqueue_all"] is True
    assert len(hashed) == 120
    assert len(set(hashed)) == 120
    assert walked_roots == [str(raw_dir.resolve())]
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest'")
        .fetchone()[0]
        == 120
    )


def test_raw_full_scan_preserves_event_arriving_during_inventory(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_ingest
    from vector_lake.watchdog_app import RawWatchdogHandler

    raw_dir = isolated_memory / "raw"
    for index in range(10):
        (raw_dir / f"initial-{index:02d}.txt").write_text(
            f"initial payload {index}",
            encoding="utf-8",
        )
    config_root = isolated_memory / "concurrent-extension"
    config_root.mkdir()
    (config_root / "config.json").write_text(
        json.dumps(
            {
                "target_directories": [str(raw_dir)],
                "exclude_paths": [],
                "supported_extensions": [".txt"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_ingest, "get_extension_root", lambda: config_root)
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "isolated test ingest instructions",
    )
    real_revision = tool_ingest.stable_raw_revision
    inventory_hashed = threading.Event()
    release_inventory = threading.Event()
    hashed = []

    def pausing_revision(filepath, *args, **kwargs):
        result = real_revision(filepath, *args, **kwargs)
        hashed.append(str(result.path))
        if len(hashed) == 10:
            inventory_hashed.set()
            if not release_inventory.wait(timeout=5):
                raise TimeoutError("test did not release startup inventory")
        return result

    real_prepare = tool_ingest.prepare_ingest_batch
    calls = []

    def recording_prepare(*args, **kwargs):
        calls.append((args, dict(kwargs)))
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(tool_ingest, "stable_raw_revision", pausing_revision)
    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", recording_prepare)
    handler = RawWatchdogHandler()
    idle = False
    try:
        handler.request_full_scan()
        assert inventory_hashed.wait(timeout=5)
        late = raw_dir / "late-event.txt"
        late.write_text("late payload", encoding="utf-8")
        handler.handle_event(SimpleNamespace(is_directory=False, src_path=str(late)))
        release_inventory.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with handler.lock:
                idle = (
                    len(calls) >= 2
                    and handler.sync_future is None
                    and not handler.pending_paths
                    and not handler.pending_overflow
                )
            if idle:
                break
            time.sleep(0.01)
    finally:
        release_inventory.set()
        clean_shutdown = handler.shutdown(timeout_seconds=2)

    assert idle is True
    assert clean_shutdown is True
    assert sum(call[1].get("_enqueue_all") is True for call in calls) == 1
    assert sum(not call[1].get("_enqueue_all", False) for call in calls) == 1
    assert len(hashed) == 11
    assert len(set(hashed)) == 11
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest'")
        .fetchone()[0]
        == 11
    )


def test_raw_watchdog_processes_quick_same_path_revision_and_supersedes_old_job(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_ingest, watchdog_app
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setattr(watchdog_app, "DEBOUNCE_SECONDS", 60)
    source = isolated_memory / "raw" / "quick-revision.txt"
    source.write_text("revision one", encoding="utf-8")
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "isolated instructions",
    )
    db_store.init_db()
    handler = RawWatchdogHandler()

    def wait_for_idle(expected_jobs):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with handler.lock:
                idle = (
                    handler.sync_future is None
                    and not handler.pending_paths
                    and not handler.pending_overflow
                )
            count = (
                db_store.get_connection()
                .execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest'")
                .fetchone()[0]
            )
            if idle and count == expected_jobs:
                return True
            time.sleep(0.01)
        return False

    try:
        handler.handle_event(SimpleNamespace(is_directory=False, src_path=str(source)))
        assert wait_for_idle(1)
        source.write_text("revision two is current", encoding="utf-8")
        current_hash = tool_ingest.calculate_hash(str(source))
        handler.handle_event(SimpleNamespace(is_directory=False, src_path=str(source)))
        assert wait_for_idle(2)
    finally:
        assert handler.shutdown(timeout_seconds=2)

    rows = (
        db_store.get_connection()
        .execute(
            "SELECT status, idempotency_key, payload FROM jobs "
            "WHERE task_type = 'ingest' ORDER BY created_at, job_id"
        )
        .fetchall()
    )
    decoded = [(row, json.loads(row["payload"])) for row in rows]
    old_row, old_payload = next(
        item for item in decoded if item[1]["hash"] != current_hash
    )
    new_row, new_payload = next(
        item for item in decoded if item[1]["hash"] == current_hash
    )
    assert old_payload["hash"] != new_payload["hash"]
    assert old_row["status"] == "superseded"
    assert old_row["idempotency_key"] is None
    assert new_row["status"] == "queued"
    assert new_row["idempotency_key"]


def test_raw_handler_shutdown_has_a_hard_deadline():
    from vector_lake.watchdog_app import RawWatchdogHandler

    release = threading.Event()
    started = threading.Event()

    def blocked_work():
        started.set()
        release.wait(2)

    handler = RawWatchdogHandler()
    handler._run_ingest = lambda _paths, _overflow: blocked_work()
    with handler.lock:
        handler._queue_batch_locked(["blocked-source"], False)
        handler._submit_pending_locked()
        future = handler.sync_future
        worker = handler.sync_thread
    assert future is not None
    assert worker is not None and worker.daemon is True
    assert started.wait(timeout=1)
    try:
        before = time.monotonic()
        clean = handler.shutdown(timeout_seconds=0.05)
        elapsed = time.monotonic() - before

        assert clean is False
        assert elapsed < 0.5
    finally:
        release.set()
        future.result(timeout=1)
        handler.shutdown(timeout_seconds=0.5)


def test_auto_ingest_drain_holds_shutdown_until_finalizer_returns():
    from vector_lake import watchdog_app

    release = threading.Event()
    finalizer = threading.Thread(target=release.wait, daemon=False)
    finalizer.start()
    result = []

    def drain():
        result.extend(
            watchdog_app._drain_auto_ingest_worker(
                {"auto_ingest": finalizer},
                ["auto_ingest"],
            )
        )

    waiter = threading.Thread(target=drain)
    waiter.start()
    time.sleep(0.05)
    assert waiter.is_alive()
    release.set()
    waiter.join(timeout=2)
    finalizer.join(timeout=2)
    assert not waiter.is_alive()
    assert result == []


def test_auto_ingest_drain_has_a_hard_deadline():
    from vector_lake import watchdog_app

    release = threading.Event()
    finalizer = threading.Thread(target=release.wait, daemon=False)
    finalizer.start()
    try:
        before = time.monotonic()
        remaining = watchdog_app._drain_auto_ingest_worker(
            {"auto_ingest": finalizer},
            ["auto_ingest"],
            timeout_seconds=0.05,
        )
        elapsed = time.monotonic() - before

        assert remaining == ["auto_ingest"]
        assert elapsed < 0.5
    finally:
        release.set()
        finalizer.join(timeout=2)


def test_raw_event_logging_aggregates_bursts_in_a_small_window(
    monkeypatch,
    caplog,
):
    from vector_lake import watchdog_app

    moments = iter((1.0, 1.01, 1.02, 1.2))
    monkeypatch.setattr(
        watchdog_app,
        "time",
        SimpleNamespace(monotonic=lambda: next(moments)),
    )
    monkeypatch.setattr(watchdog_app, "_raw_event_log_window_seconds", lambda: 0.1)
    monkeypatch.setattr(watchdog_app, "_RAW_EVENT_LOG_WINDOW_STARTED", 0.0)
    monkeypatch.setattr(watchdog_app, "_RAW_EVENT_LOG_SUPPRESSED", 0)
    monkeypatch.setattr(watchdog_app, "_RAW_EVENT_LOG_FILENAMES", set())

    with caplog.at_level("INFO", logger="watchdog_sync"):
        watchdog_app._log_raw_event("one.md")
        watchdog_app._log_raw_event("one.md")
        watchdog_app._log_raw_event("two.md")
        watchdog_app._log_raw_event("three.md")

    detail = [
        record.message
        for record in caplog.records
        if record.message.startswith("Raw source modified:")
    ]
    aggregate = [
        record.message
        for record in caplog.records
        if record.message.startswith("Raw source events aggregated:")
    ]
    assert len(detail) == 2
    assert aggregate == [
        "Raw source events aggregated: suppressed=2 unique_files=2 "
        "window_seconds=0.20"
    ]


@pytest.mark.parametrize("drain_publish_raises", [False, True])
def test_watchdog_drains_auto_before_stopping_required_peer_workers(
    isolated_memory,
    monkeypatch,
    drain_publish_raises,
):
    from vector_lake import (
        auto_ingest_worker,
        db_store,
        ingest_worker,
        runtime_health,
        watchdog_app,
    )
    from vector_lake.watchdog_status import write_status as publish_watchdog_status

    supervisor_stop = threading.Event()
    auto_started = threading.Event()
    auto_stop_seen = threading.Event()
    release_auto = threading.Event()
    peer_events = []
    auto_events = []
    order = []
    statuses = []
    errors = []
    missing = isolated_memory / "not-watched"
    injected_drain_failure = False

    db_store.init_db()
    meta_dir = isolated_memory / "wiki" / ".meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "auto_ingest_config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "allow_model_processing_raw_text": True,
                "timeout_seconds": 1200,
            }
        ),
        encoding="utf-8",
    )
    for component in ("watchdog", "outbox", "scheduler", "ingest"):
        assert publish_watchdog_status(
            "idle",
            0,
            0,
            f"{component} heartbeat",
            "",
            component=component,
        )
    assert publish_watchdog_status(
        "processing",
        1,
        0,
        "Automatic ingest finalizing test job",
        "",
        component="auto_ingest",
    )

    class FakeObserver:
        def __init__(self):
            self.alive = False

        def schedule(self, *_args, **_kwargs):
            return None

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout=None):
            return None

    def peer_worker(event):
        peer_events.append(event)
        assert event.wait(3)
        component = {
            "vector-lake-outbox-worker": "outbox",
            "vector-lake-scheduled-lint-worker": "scheduler",
            "vector-lake-ingest-worker": "ingest",
        }[threading.current_thread().name]
        assert publish_watchdog_status(
            "stopped",
            0,
            0,
            f"{component} stopped",
            "",
            component=component,
        )
        order.append("peer_stopped")

    def auto_worker(event):
        auto_events.append(event)
        auto_started.set()
        assert event.wait(3)
        auto_stop_seen.set()
        assert release_auto.wait(3)
        assert publish_watchdog_status(
            "stopped",
            0,
            0,
            "Automatic ingest stopped",
            "",
            component="auto_ingest",
        )
        order.append("auto_returned")

    def observed_write_status(
        state,
        task_queue_size,
        index_queue_size,
        current_action="",
        last_error="",
        component="watchdog",
    ):
        nonlocal injected_drain_failure
        statuses.append((component, state))
        published = publish_watchdog_status(
            state,
            task_queue_size,
            index_queue_size,
            current_action,
            last_error,
            component=component,
        )
        if (
            drain_publish_raises
            and state == "draining"
            and not injected_drain_failure
        ):
            injected_drain_failure = True
            raise OSError("injected drain heartbeat publish failure")
        return published

    monkeypatch.setattr(watchdog_app, "Observer", FakeObserver)
    monkeypatch.setattr(
        watchdog_app,
        "_watch_directories",
        lambda *_args: {"wiki": missing, "diary": missing, "raw": missing},
    )
    monkeypatch.setattr(watchdog_app, "index_worker_loop", peer_worker)
    monkeypatch.setattr(watchdog_app, "scheduled_lint_loop", peer_worker)
    monkeypatch.setattr(ingest_worker, "start_worker", peer_worker)
    monkeypatch.setattr(
        auto_ingest_worker,
        "start_auto_ingest_worker",
        auto_worker,
    )
    monkeypatch.setattr(
        watchdog_app,
        "write_status",
        observed_write_status,
    )
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS", "0.05")
    monkeypatch.setenv("VECTOR_LAKE_WATCHDOG_SHUTDOWN_TIMEOUT_SECONDS", "1")

    def run_watchdog():
        try:
            watchdog_app._start_watchdog_locked(supervisor_stop)
        except BaseException as exc:
            errors.append(exc)

    watchdog_thread = threading.Thread(target=run_watchdog, daemon=False)
    watchdog_thread.start()
    assert auto_started.wait(2)
    deadline = time.monotonic() + 2
    while len(peer_events) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(peer_events) == 3

    supervisor_stop.set()
    assert auto_stop_seen.wait(2)
    assert all(not event.is_set() for event in peer_events)
    assert "peer_stopped" not in order
    deadline = time.monotonic() + 2
    while ("watchdog", "draining") not in statuses and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ("watchdog", "draining") in statuses
    drain_health = runtime_health.assess_runtime_health()
    effective_statuses = drain_health["detail"][
        "watchdog_component_effective_statuses"
    ]
    assert all(
        effective_statuses[component] != "stopped"
        for component in ("watchdog", "outbox", "scheduler", "ingest", "auto_ingest")
    )
    assert not any(
        issue.startswith("watchdog_unhealthy:")
        or issue.startswith("watchdog_component_stale:")
        for issue in drain_health["issues"]
    )
    runtime_health.enforce_runtime_write_health()

    release_auto.set()
    watchdog_thread.join(timeout=3)

    assert errors == []
    assert not watchdog_thread.is_alive()
    assert len(auto_events) == 1
    assert len({id(event) for event in peer_events}) == 1
    assert auto_events[0] is not peer_events[0]
    assert order[0] == "auto_returned"
    assert order.count("peer_stopped") == 3
    final_status = json.loads(get_status_file().read_text(encoding="utf-8"))
    assert all(
        final_status["components"][component]["status"] == "stopped"
        for component in ("outbox", "scheduler", "ingest", "auto_ingest")
    )
    expected_watchdog_status = "halted" if drain_publish_raises else "stopped"
    assert final_status["components"]["watchdog"]["status"] == (
        expected_watchdog_status
    )
    assert injected_drain_failure is drain_publish_raises


def test_shared_stop_event_prevents_ingest_dispatch(monkeypatch):
    from vector_lake import ingest_worker

    statuses = []
    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(
        ingest_worker,
        "process_jobs",
        lambda: (_ for _ in ()).throw(AssertionError("dispatch should not run")),
    )
    monkeypatch.setattr(
        ingest_worker,
        "write_status",
        lambda state, *_args, component="watchdog", **_kwargs: statuses.append(
            (component, state)
        ),
    )

    ingest_worker.start_worker(stop_event)

    assert statuses == [("ingest", "idle"), ("ingest", "stopped")]


def test_stopped_raw_handler_rejects_new_events(monkeypatch, tmp_path):
    from vector_lake.watchdog_app import RawWatchdogHandler

    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    stop_event = threading.Event()
    handler = RawWatchdogHandler(stop_event=stop_event)
    stop_event.set()
    monkeypatch.setattr(
        handler,
        "_run_ingest",
        lambda *_args: (_ for _ in ()).throw(AssertionError("ingest should not run")),
    )

    handler.handle_event(SimpleNamespace(is_directory=False, src_path=str(source)))

    assert handler.sync_future is None
    assert handler.pending_paths == set()
