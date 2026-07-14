from pathlib import Path

import pytest

from vector_lake import db_store, wiki_utils


@pytest.fixture(autouse=True)
def reset_database_connection():
    db_store.close_connection()
    wiki_utils._META_DIR_CACHE = None
    yield
    db_store.close_connection()
    wiki_utils._META_DIR_CACHE = None


@pytest.fixture
def isolated_memory(tmp_path: Path, monkeypatch):
    """Give each persistence test its own MEMORY tree and SQLite connection."""
    db_store.close_connection()
    wiki_utils._META_DIR_CACHE = None
    memory_dir = tmp_path / "MEMORY"
    (memory_dir / "wiki").mkdir(parents=True)
    (memory_dir / "raw").mkdir(parents=True)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    yield memory_dir
    db_store.close_connection()
    wiki_utils._META_DIR_CACHE = None
