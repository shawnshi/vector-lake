from pathlib import Path

from vector_lake import db_store, wiki_utils


def test_autouse_fixture_never_uses_operator_memory(tmp_path: Path):
    expected = (tmp_path / "MEMORY").resolve()
    live_defaults = {
        (Path.home() / "MEMORY").resolve(),
        (Path.home() / ".gemini" / "MEMORY").resolve(),
    }

    assert wiki_utils.get_memory_dir() == expected
    assert wiki_utils.get_memory_dir() not in live_defaults
    assert db_store.get_db_path().resolve() == (
        expected / "wiki" / ".meta" / "vector_lake.db"
    ).resolve()


def test_library_fallback_memory_root_is_host_neutral(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_MEMORY_DIR", raising=False)

    assert wiki_utils.get_memory_dir() == (Path.home() / "MEMORY").resolve()
