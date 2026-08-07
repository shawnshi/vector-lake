import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

from vector_lake.heavy_task_gate import (
    HeavyTaskBusy,
    HeavyTaskClass,
    heavy_task,
    heavy_task_gate_status,
)
import vector_lake.heavy_task_gate as heavy_task_gate


ROOT = Path(__file__).resolve().parents[1]


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(ROOT) if not existing else os.pathsep.join((str(ROOT), existing))
    )
    return env


def _start_holder(meta_dir: Path, *, crash_on_input: bool = False):
    ready_path = meta_dir / f".holder-ready-{uuid.uuid4().hex}"
    ending = (
        "sys.stdin.readline(); os._exit(23)"
        if crash_on_input
        else "sys.stdin.readline()"
    )
    code = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "from vector_lake.heavy_task_gate import heavy_task\n"
        "meta=sys.argv[1]\n"
        "ready=Path(sys.argv[2])\n"
        "with heavy_task('scan','child-holder',origin='test',"
        "wait_timeout_seconds=0,warn_after_seconds=60,meta_dir=meta):\n"
        " ready.write_text('READY', encoding='utf-8')\n"
        f" {ending}\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(meta_dir), str(ready_path)],
        cwd=ROOT,
        env=_subprocess_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready_path.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    if not ready_path.exists():
        process.kill()
        process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr is not None else ""
        pytest.fail(f"holder did not start: stderr={stderr!r}")
    for attempt in range(10):
        try:
            ready_path.unlink()
            break
        except PermissionError:
            if attempt == 9:
                pytest.fail(f"holder readiness marker stayed locked: {ready_path}")
            time.sleep(0.02)
    return process


def _finish_holder(process: subprocess.Popen) -> None:
    if process.poll() is None and process.stdin is not None:
        process.stdin.write("\n")
        process.stdin.flush()
    try:
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_default_gate_is_scoped_to_canonical_meta_root(isolated_memory):
    with heavy_task(
        HeavyTaskClass.SCAN,
        "canonical-scan",
        origin="pytest",
        wait_timeout_seconds=0,
        warn_after_seconds=30,
    ) as lease:
        status = heavy_task_gate_status()
        assert status["physical_state"] == "locked"
        assert status["owned_by_current_thread"] is True
        assert status["current"]["task_id"] == lease.task_id
        assert status["current"]["task_class"] == "scan"
        assert status["current"]["operation"] == "canonical-scan"
        assert status["current"]["origin"] == "pytest"
        assert status["current"]["overdue"] is False

    status_path = isolated_memory / "wiki" / ".meta" / ".heavy-task-status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["current"] is None
    assert payload["last"]["task_id"] == lease.task_id
    assert payload["last"]["outcome"] == "completed"
    assert not list(status_path.parent.glob(".heavy-task-status.json.*.tmp"))
    with pytest.raises(RuntimeError, match="single-use"):
        lease.__enter__()


def test_status_probe_does_not_create_uninitialized_meta_root(tmp_path):
    meta_dir = tmp_path / "not-created" / "meta"

    status = heavy_task_gate_status(meta_dir=meta_dir)

    assert status["physical_state"] == "free"
    assert status["initialized"] is False
    assert meta_dir.exists() is False


def test_same_thread_reenters_while_other_thread_gets_structured_busy(tmp_path):
    meta_dir = tmp_path / "meta"
    results = []

    with heavy_task(
        "projection",
        "outer",
        origin="pytest",
        wait_timeout_seconds=0,
        meta_dir=meta_dir,
    ) as outer:
        with heavy_task(
            "scan",
            "nested",
            origin="pytest",
            wait_timeout_seconds=0,
            meta_dir=meta_dir,
        ) as nested:
            assert nested.task_id == outer.task_id

            def contend():
                try:
                    with heavy_task(
                        "maintenance",
                        "contender",
                        origin="thread",
                        wait_timeout_seconds=0.05,
                        meta_dir=meta_dir,
                    ):
                        results.append("unexpected-acquire")
                except HeavyTaskBusy as exc:
                    results.append(exc.to_dict())

            thread = threading.Thread(target=contend, name="gate-contender")
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive()

    assert len(results) == 1
    busy = results[0]
    assert isinstance(busy, dict)
    assert busy["error"] == "heavy_task_busy"
    assert busy["requested"]["task_class"] == "maintenance"
    assert busy["requested"]["operation"] == "contender"
    assert busy["gate"]["physical_state"] == "locked"
    assert busy["gate"]["current"]["task_id"] == outer.task_id


def test_different_meta_roots_do_not_share_capacity(tmp_path):
    meta_a = tmp_path / "a" / "meta"
    meta_b = tmp_path / "b" / "meta"

    with heavy_task(
        "scan",
        "root-a",
        origin="pytest",
        wait_timeout_seconds=0,
        meta_dir=meta_a,
    ):
        with heavy_task(
            "embedding",
            "root-b",
            origin="pytest",
            wait_timeout_seconds=0,
            meta_dir=meta_b,
        ):
            assert heavy_task_gate_status(meta_dir=meta_a)["physical_state"] == "locked"
            assert heavy_task_gate_status(meta_dir=meta_b)["physical_state"] == "locked"


def test_cross_process_contention_is_bounded_and_reports_owner(tmp_path):
    meta_dir = tmp_path / "meta"
    holder = _start_holder(meta_dir)
    try:
        started = time.monotonic()
        with pytest.raises(HeavyTaskBusy) as captured:
            with heavy_task(
                "maintenance",
                "parent-contender",
                origin="pytest",
                wait_timeout_seconds=0.15,
                meta_dir=meta_dir,
            ):
                pass
        elapsed = time.monotonic() - started
        assert 0.10 <= elapsed < 2.0
        detail = captured.value.to_dict()
        assert detail["gate"]["physical_state"] == "locked"
        assert detail["gate"]["current"]["operation"] == "child-holder"
        assert detail["gate"]["current"]["pid"] == holder.pid
    finally:
        _finish_holder(holder)


def test_process_crash_releases_os_lock_and_marks_stale_metadata(tmp_path):
    meta_dir = tmp_path / "meta"
    holder = _start_holder(meta_dir, crash_on_input=True)
    assert holder.stdin is not None
    holder.stdin.write("\n")
    holder.stdin.flush()
    assert holder.wait(timeout=5) == 23

    stale = heavy_task_gate_status(meta_dir=meta_dir)
    assert stale["physical_state"] == "free"
    assert stale["stale_metadata"] is True
    assert stale["current"]["operation"] == "child-holder"

    with heavy_task(
        "projection",
        "recovery",
        origin="pytest",
        wait_timeout_seconds=1,
        meta_dir=meta_dir,
    ):
        recovered = heavy_task_gate_status(meta_dir=meta_dir)
        assert recovered["physical_state"] == "locked"
        assert recovered["stale_metadata"] is False
        assert recovered["current"]["operation"] == "recovery"
        assert recovered["last"]["outcome"] == "abandoned"
        assert recovered["last"]["operation"] == "child-holder"


def test_release_error_still_releases_process_thread_lock(tmp_path, monkeypatch):
    meta_dir = tmp_path / "meta"
    manager = heavy_task_gate._manager_for(meta_dir)
    original_release = manager._file_lock.release
    release_calls = 0

    def release_then_fail_once(*args, **kwargs):
        nonlocal release_calls
        release_calls += 1
        original_release(*args, **kwargs)
        if release_calls == 1:
            raise OSError("injected physical release failure")

    monkeypatch.setattr(manager._file_lock, "release", release_then_fail_once)

    with pytest.raises(OSError, match="injected physical release failure"):
        with heavy_task(
            "scan",
            "release-error",
            origin="pytest",
            wait_timeout_seconds=0,
            meta_dir=meta_dir,
        ):
            pass

    outcome = []

    def acquire_after_failure():
        with heavy_task(
            "scan",
            "release-recovery",
            origin="thread",
            wait_timeout_seconds=0.5,
            meta_dir=meta_dir,
        ):
            outcome.append("acquired")

    contender = threading.Thread(target=acquire_after_failure)
    contender.start()
    contender.join(timeout=2)

    assert not contender.is_alive()
    assert outcome == ["acquired"]


def test_acquire_rollback_release_error_still_releases_thread_lock(
    tmp_path,
    monkeypatch,
):
    meta_dir = tmp_path / "meta"
    manager = heavy_task_gate._manager_for(meta_dir)
    original_release = manager._file_lock.release
    original_write = heavy_task_gate._atomic_write_json
    release_calls = 0
    write_calls = 0

    def release_then_fail_once(*args, **kwargs):
        nonlocal release_calls
        release_calls += 1
        original_release(*args, **kwargs)
        if release_calls == 1:
            raise OSError("injected rollback release failure")

    def fail_owner_publish_once(*args, **kwargs):
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            raise OSError("injected owner-state failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(manager._file_lock, "release", release_then_fail_once)
    monkeypatch.setattr(
        heavy_task_gate,
        "_atomic_write_json",
        fail_owner_publish_once,
    )

    with pytest.raises(OSError, match="injected rollback release failure"):
        with heavy_task(
            "scan",
            "acquire-rollback",
            origin="pytest",
            wait_timeout_seconds=0,
            meta_dir=meta_dir,
        ):
            pass

    outcome = []

    def acquire_after_failure():
        with heavy_task(
            "scan",
            "rollback-recovery",
            origin="thread",
            wait_timeout_seconds=0.5,
            meta_dir=meta_dir,
        ):
            outcome.append("acquired")

    contender = threading.Thread(target=acquire_after_failure)
    contender.start()
    contender.join(timeout=2)

    assert not contender.is_alive()
    assert outcome == ["acquired"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wait_timeout_seconds", -1),
        ("wait_timeout_seconds", float("nan")),
        ("wait_timeout_seconds", float("inf")),
        ("warn_after_seconds", 0),
        ("warn_after_seconds", float("nan")),
    ],
)
def test_invalid_timeouts_fail_closed(field, value):
    kwargs = {
        "origin": "pytest",
        "wait_timeout_seconds": 0,
        "warn_after_seconds": None,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        heavy_task("scan", "invalid-timeout", **kwargs)
