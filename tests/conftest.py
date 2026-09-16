import json
from pathlib import Path

import pytest

from vector_lake import db_store, wiki_utils


@pytest.fixture(autouse=True)
def hermetic_memory_root(tmp_path_factory, monkeypatch):
    """Never let a test touch the configured MEMORY root.

    ``config.json`` carries a per-machine ``memory_dir``, so without this every
    test that forgets ``isolated_memory`` would read and write the real
    knowledge base.  The env override wins over the config value.
    """
    memory_dir = tmp_path_factory.mktemp("hermetic") / "MEMORY"
    (memory_dir / "wiki").mkdir(parents=True)
    (memory_dir / "raw").mkdir(parents=True)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    wiki_utils.reset_memory_dir_cache()
    yield memory_dir
    db_store.close_connection()
    wiki_utils.reset_memory_dir_cache()


@pytest.fixture(autouse=True)
def reset_database_connection():
    db_store.close_connection()
    wiki_utils.reset_memory_dir_cache()
    yield
    db_store.close_connection()
    wiki_utils.reset_memory_dir_cache()


@pytest.fixture
def isolated_memory(tmp_path: Path, monkeypatch):
    """Give each persistence test its own MEMORY tree and SQLite connection."""
    db_store.close_connection()
    wiki_utils.reset_memory_dir_cache()
    memory_dir = tmp_path / "MEMORY"
    (memory_dir / "wiki").mkdir(parents=True)
    (memory_dir / "raw").mkdir(parents=True)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    yield memory_dir
    db_store.close_connection()
    wiki_utils.reset_memory_dir_cache()


def test_hermetic_root_is_not_the_configured_memory_dir(hermetic_memory_root):
    """Guard: the harness itself must not resolve to the machine's MEMORY root."""
    import os

    configured = None
    config_path = Path(__file__).resolve().parents[1] / "config.json"
    if config_path.exists():
        configured = json.loads(config_path.read_text(encoding="utf-8")).get("memory_dir")
    assert str(hermetic_memory_root) == os.environ["VECTOR_LAKE_MEMORY_DIR"]
    assert wiki_utils.get_memory_dir() == hermetic_memory_root.resolve()
    if configured:
        assert wiki_utils.get_memory_dir() != Path(configured).resolve()
