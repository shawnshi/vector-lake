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
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_DB_PATH", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_SUBAGENT_BRAIN_ROOT", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_SUBAGENT_TASK_ROOT", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    yield memory_dir
    db_store.close_all_connections()
    wiki_utils._META_DIR_CACHE = None


@pytest.fixture
def isolated_memory(isolate_test_runtime: Path):
    """Expose the per-test MEMORY tree to tests that need its path."""
    return isolate_test_runtime
