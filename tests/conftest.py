from pathlib import Path

import pytest

from vector_lake import db_store, wiki_utils


@pytest.fixture(autouse=True)
def isolate_test_runtime(tmp_path: Path, monkeypatch):
    """Keep every test away from the operator's live Vector Lake state."""
    db_store.close_all_connections()
    wiki_utils._META_DIR_CACHE = None
    memory_dir = tmp_path / "MEMORY"
    (memory_dir / "wiki").mkdir(parents=True)
    (memory_dir / "raw").mkdir(parents=True)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_dir))
    monkeypatch.setenv(
        "VECTOR_LAKE_META_DIR",
        str(memory_dir / "wiki" / ".meta"),
    )
    monkeypatch.delenv("VECTOR_LAKE_DB_PATH", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_SUBAGENT_BRAIN_ROOT", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_SUBAGENT_TASK_ROOT", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # The maintenance-backup policy is a deployment setting: an operator who pauses
    # it on the host must not silently change what the suite exercises, and several
    # tests build a real backup and then use its returned path.
    monkeypatch.delenv("VECTOR_LAKE_MAINTENANCE_BACKUP_MODE", raising=False)
    # The deterministic manual-edit quarantine and the operational-memory
    # attestation clock are in-process state, so they must not leak between tests.
    from vector_lake import watchdog_app

    watchdog_app.reset_legacy_projection_quarantine()
    watchdog_app.reset_operational_memory_attestation_clock()
    yield memory_dir
    watchdog_app.reset_legacy_projection_quarantine()
    watchdog_app.reset_operational_memory_attestation_clock()
    db_store.close_all_connections()
    wiki_utils._META_DIR_CACHE = None


@pytest.fixture
def isolated_memory(isolate_test_runtime: Path):
    """Expose the per-test MEMORY tree to tests that need its path."""
    return isolate_test_runtime
