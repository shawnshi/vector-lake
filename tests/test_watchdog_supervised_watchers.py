import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from vector_lake import thread_supervision, watchdog_app, watchdog_status


@pytest.fixture(autouse=True)
def registry():
    thread_supervision.reset()
    yield
    thread_supervision.reset()


def _heartbeat():
    tree = ast.parse(Path(watchdog_app.__file__).read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_start_watchdog_locked")
    call = next(node for node in ast.walk(function) if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "write_status"
                and any(isinstance(arg, ast.Constant) and arg.value == "Watchdog heartbeat" for arg in node.value.args))
    exec(compile(ast.Module(body=[call], type_ignores=[]), watchdog_app.__file__, "exec"), watchdog_app.__dict__)


def test_watcher_failure_survives_main_heartbeat(isolated_memory, monkeypatch):
    def broken_watch(*args, **kwargs):
        raise OSError("synthetic watcher failure")

    monkeypatch.setattr(watchdog_app, "_watch", broken_watch)
    watcher = watchdog_app._WatchLoop(SimpleNamespace(), "synthetic", False, "wiki")
    watcher.start()
    watcher.join(timeout=2)
    _heartbeat()

    status = json.loads(watchdog_status.get_status_file().read_text(encoding="utf-8"))
    assert status["status"] == "error"
    assert status["components"]["watch-wiki"]["status"] == "error"
    assert "watch-wiki" in thread_supervision.snapshot()


def test_restart_recovers_and_shutdown_joins_replacement(isolated_memory, monkeypatch):
    calls = []
    restarted = threading.Event()

    def watch(*args, stop_event, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("synthetic first failure")
        restarted.set()
        stop_event.wait(2)
        return iter(())

    monkeypatch.setattr(watchdog_app, "_watch", watch)
    watcher = watchdog_app._WatchLoop(SimpleNamespace(), "synthetic", True, "raw")
    watcher.start()
    watcher.join(timeout=2)
    assert thread_supervision.supervise_once()["watch-raw"] == "restarted"
    assert restarted.wait(2)
    assert thread_supervision.snapshot()["watch-raw"]["alive"]
    _heartbeat()
    status = json.loads(watchdog_status.get_status_file().read_text(encoding="utf-8"))
    assert status["components"]["watch-raw"]["status"] == "idle"

    watcher.stop()
    watcher.join(timeout=2)
    assert not thread_supervision.snapshot()["watch-raw"]["alive"]
    assert thread_supervision.supervise_once()["watch-raw"] == "finished"


def test_watcher_restart_budget_remains_bounded(isolated_memory, monkeypatch):
    def broken_watch(*args, **kwargs):
        raise OSError("synthetic repeated failure")

    monkeypatch.setattr(watchdog_app, "_watch", broken_watch)
    watcher = watchdog_app._WatchLoop(SimpleNamespace(), "synthetic", False, "wiki")
    watcher.start()
    watcher.join(timeout=2)
    for _ in range(thread_supervision.DEFAULT_RESTART_LIMIT):
        assert thread_supervision.supervise_once()["watch-wiki"] == "restarted"
        watcher.join(timeout=2)
    assert thread_supervision.supervise_once()["watch-wiki"] == "dead"
    _heartbeat()
    status = json.loads(watchdog_status.get_status_file().read_text(encoding="utf-8"))
    assert status["status"] == "error"
