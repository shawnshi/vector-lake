"""Runtime repairs on synthetic, hermetic data only: no provider calls or real PIDs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from vector_lake import db_store, memory_gram_index, tool_ingest
from vector_lake.process_control import run_contained, start_contained_python, stop_contained
from vector_lake.runtime_environment import configure_numeric_threads


def _alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            assert api.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("shell", [False, True])
def test_timeout_bounds_pipe_drain_and_ends_owned_descendants(tmp_path, shell):
    receipt = tmp_path / "pid.json"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess,sys,json,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        f"open({str(receipt)!r},'w').write(json.dumps({{'pid':p.pid}}))\n"
        "time.sleep(30)\n", encoding="utf-8")
    command = [sys.executable, str(script)]
    if shell:
        command = subprocess.list2cmdline(command) if os.name == "nt" else ' '.join(__import__('shlex').quote(x) for x in command)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_contained(command, timeout=1, shell=shell)
    assert time.monotonic() - started < 5
    assert receipt.exists(), "fixture must have actually launched its child"
    pid = json.loads(receipt.read_text())["pid"]
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.02)
    assert not _alive(pid), "owned descendant must not survive a timeout"


def test_python_entry_keeps_pid_identity_and_closes_descendants(tmp_path):
    receipt = tmp_path / "ids.json"
    script = tmp_path / "entry.py"
    script.write_text(
        "import os,sys,subprocess,json,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        f"open({str(receipt)!r},'w').write(json.dumps({{'parent':os.getpid(),'child':p.pid}}))\n"
        "time.sleep(30)\n", encoding="utf-8")
    process = start_contained_python([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if receipt.exists() and receipt.stat().st_size:
                break
            time.sleep(0.02)
        ids = json.loads(receipt.read_text())
        assert ids["parent"] == process.pid
    finally:
        stop_contained(process)
        process.wait(timeout=5)
    for _ in range(50):
        if not _alive(ids["child"]):
            break
        time.sleep(0.02)
    assert not _alive(ids["child"])


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership/crash semantics")
def test_owner_hard_crash_closes_nested_supervisor_runner_model_jobs(tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    entry = tmp_path / "nested.py"
    entry.write_text(
        f"import sys,os,time,json;sys.path.insert(0,{root!r})\n"
        "from pathlib import Path\n"
        "from vector_lake.process_control import start_contained_python\n"
        "level=int(sys.argv[1]);folder=Path(sys.argv[2])\n"
        "if level<2: child=start_contained_python([sys.executable,__file__,str(level+1),str(folder)])\n"
        "(folder/('level'+str(level)+'.json')).write_text(json.dumps({'pid':os.getpid()}))\n"
        "time.sleep(30)\n", encoding="utf-8")
    owner = tmp_path / "owner.py"
    owner.write_text(
        f"import sys,os,time;sys.path.insert(0,{root!r})\n"
        "from pathlib import Path\n"
        "from vector_lake.process_control import start_contained_python\n"
        f"child=start_contained_python([sys.executable,{str(entry)!r},'0',{str(tmp_path)!r}])\n"
        f"ready=Path({str(tmp_path / 'level2.json')!r})\n"
        "deadline=time.monotonic()+8\n"
        "while not ready.exists() and time.monotonic()<deadline:time.sleep(0.01)\n"
        "os._exit(17 if ready.exists() else 18)\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(owner)], capture_output=True, timeout=12)
    assert result.returncode == 17, result.stderr
    pids = [json.loads((tmp_path / f"level{i}.json").read_text())["pid"] for i in range(3)]
    for _ in range(100):
        if not any(_alive(pid) for pid in pids):
            break
        time.sleep(0.02)
    assert not any(_alive(pid) for pid in pids), "owner hard exit must close every nested Job"


def test_numeric_preset_is_local_and_respects_explicit_values():
    env = {"OPENBLAS_NUM_THREADS": "4"}
    configure_numeric_threads(env)
    assert env["OPENBLAS_NUM_THREADS"] == "4"
    assert env["OMP_NUM_THREADS"] == "1"
    disabled = {"VECTOR_LAKE_NUMERIC_THREADS": "0"}
    configure_numeric_threads(disabled)
    assert "OMP_NUM_THREADS" not in disabled
    with pytest.raises(ValueError):
        configure_numeric_threads({"VECTOR_LAKE_NUMERIC_THREADS": "-1"})


def test_invalid_checksum_is_withheld_without_rewriting_history(isolated_memory, monkeypatch):
    raw = isolated_memory / "raw" / "synthetic.md"
    raw.write_text("Synthetic public input.\n", encoding="utf-8")
    db_store.init_db()
    s = raw.stat()
    old_hash = "d41d8cd98f00b204e9800998ecf8427e"
    db_store.mark_file_processed(str(raw), old_hash, mtime_ns=s.st_mtime_ns, size=s.st_size)
    monkeypatch.setattr(tool_ingest, "load_config", lambda: {"target_dirs": ["raw"], "supported_extensions": [".md"]})
    result = tool_ingest.prepare_ingest_batch()
    assert "quarantined" in result
    row = db_store.get_connection().execute("SELECT file_hash FROM processed_files WHERE filepath=?", (str(raw),)).fetchone()
    assert row[0] == old_hash
    assert db_store.processed_checksum_anomalies() == 1
    assert db_store.get_connection().execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_model_budget_rejects_nonfinite_or_nonpositive_values(monkeypatch, value):
    from vector_lake.process_control import model_timeout_seconds
    monkeypatch.setenv("VECTOR_LAKE_RUNNER_MODEL_TIMEOUT", value)
    with pytest.raises(ValueError):
        model_timeout_seconds()


def test_expired_claim_does_not_launch_or_modify_new_owner(monkeypatch):
    from scripts import ingest_runner
    def forbidden(*args, **kwargs):
        pytest.fail("expired claim must not execute or change job history")
    monkeypatch.setattr(ingest_runner, "run_model", forbidden)
    monkeypatch.setattr(ingest_runner, "record_ingest_failure", forbidden)
    res, err = ingest_runner._process_task(
        {"job_id": "synthetic", "lease_until": "2000-01-01T00:00:00+00:00", "task_packet": {}},
        False, "unused", {},
    )
    assert res["needs-model"] == 1
    assert "lease recovery" in err


def test_legitimate_empty_to_nonempty_edit_dispatches_without_rewriting_history(isolated_memory, monkeypatch):
    from tests.test_mutation_coordinator import _write_purpose_contract
    _write_purpose_contract(isolated_memory)
    raw = isolated_memory / "raw" / "synthetic.md"
    raw.write_text("", encoding="utf-8")
    db_store.init_db()
    empty_stat = raw.stat()
    empty_digest = tool_ingest.calculate_hash(str(raw))
    db_store.mark_file_processed(str(raw), empty_digest,
                                 mtime_ns=empty_stat.st_mtime_ns, size=empty_stat.st_size)
    conn = db_store.get_connection()
    before = dict(conn.execute("SELECT * FROM processed_files WHERE filepath=?", (str(raw),)).fetchone())
    raw.write_text("Synthetic newly added source content.\n", encoding="utf-8")
    monkeypatch.setattr(tool_ingest, "load_config", lambda: {"target_dirs": ["raw"], "supported_extensions": [".md"]})
    result = tool_ingest.prepare_ingest_batch(batch_size=1)
    assert "quarantined" not in result
    job = conn.execute("SELECT payload FROM jobs WHERE task_type='ingest'").fetchone()
    assert job is not None, result
    payload = json.loads(job[0])
    assert payload["hash"] == tool_ingest.calculate_hash(str(raw)) != empty_digest
    assert dict(conn.execute("SELECT * FROM processed_files WHERE filepath=?", (str(raw),)).fetchone()) == before
    assert db_store.processed_checksum_anomalies() == 0


def test_versioned_sha256_observation_is_not_misclassified_as_bad_md5(isolated_memory, monkeypatch):
    import hashlib
    raw = isolated_memory / "raw" / "synthetic.md"
    raw.write_text("Synthetic versioned digest input.\n", encoding="utf-8")
    db_store.init_db()
    stat = raw.stat()
    digest = "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest()
    db_store.mark_file_processed(str(raw), digest, mtime_ns=stat.st_mtime_ns, size=stat.st_size)
    monkeypatch.setattr(tool_ingest, "load_config", lambda: {"target_dirs": ["raw"], "supported_extensions": [".md"]})
    result = tool_ingest.prepare_ingest_batch()
    assert "quarantined" not in result
    assert db_store.processed_checksum_anomalies() == 0
    assert db_store.get_connection().execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_checkpoint_busy_is_not_a_success():
    from vector_lake.watchdog_app import checkpoint_wal
    class Connection:
        def __init__(self, row):
            self.row = row
        def execute(self, query):
            assert query == "PRAGMA wal_checkpoint(TRUNCATE)"
            return self
        def fetchone(self):
            return self.row
    assert checkpoint_wal(Connection((0, 0, 0)))["busy"] == 0
    for row in [(1, 10, 2), (0, 10, 2), None]:
        with pytest.raises(RuntimeError):
            checkpoint_wal(Connection(row))


def test_dirty_base_age_triggers_without_a_write_threshold(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr(memory_gram_index, "gram_index_state", lambda: {"ready": True, "updated_at": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(memory_gram_index, "writes_since_rebuild", lambda conn: 1)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_GRAM_MAX_BASE_AGE_SECONDS", "3600")
    assert "age" in memory_gram_index.rebuild_due_reason()
    monkeypatch.setattr(memory_gram_index, "writes_since_rebuild", lambda conn: 0)
    assert memory_gram_index.rebuild_due_reason() is None


def test_naive_gram_timestamp_is_utc_not_local_time(isolated_memory, monkeypatch):
    from datetime import datetime, timezone
    db_store.init_db()
    stamp = "2026-10-05T12:00:00"
    epoch = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(memory_gram_index.time, "time", lambda: epoch + 1)
    monkeypatch.setattr(memory_gram_index, "gram_index_state", lambda: {"ready": True, "updated_at": stamp})
    monkeypatch.setattr(memory_gram_index, "writes_since_rebuild", lambda conn: 1)
    monkeypatch.setattr(memory_gram_index, "_searches_since_last_rebuild", lambda: 0)
    assert memory_gram_index.rebuild_due_reason() is None


def test_dispatch_recomputes_candidates_even_for_current_contract(isolated_memory, monkeypatch):
    raw = isolated_memory / "raw" / "synthetic.md"
    raw.write_text("Synthetic public input.\n", encoding="utf-8")
    payload = {"filepath": str(raw), "hash": tool_ingest.calculate_hash(str(raw)),
               "canonical_name": "Source_synthetic.md", "ingest_contract_version": tool_ingest.INGEST_CONTRACT_VERSION,
               "integration_candidates": [{"target_hash": "old"}]}
    monkeypatch.setattr(tool_ingest, "ingest_context_and_candidates", lambda _: ("fresh", [{"target_hash": "new"}]))
    monkeypatch.setattr(tool_ingest, "_build_ingest_instructions", lambda *a, **k: k["index_context"])
    monkeypatch.setattr(tool_ingest.governance_store, "canonical_page_versions", lambda _: {"Source_synthetic": "source-new"})
    refreshed = tool_ingest.refresh_ingest_dispatch_payload(payload)
    assert refreshed["integration_candidates"] == [{"target_hash": "new"}]
    assert refreshed["source_hash"] == "source-new"
    assert payload["integration_candidates"] == [{"target_hash": "old"}]
    raw.write_text("Changed public input.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        tool_ingest.refresh_ingest_dispatch_payload(payload)
