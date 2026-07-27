import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from vector_lake import db_store
from vector_lake.runtime_health import assess_runtime_health
from vector_lake.watchdog_status import get_status_file, write_status


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
        issue.startswith("watchdog_component_stale:")
        for issue in health["issues"]
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
        lambda: {"wiki": missing, "diary": missing, "raw": missing},
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
    }


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
