from pathlib import Path

from vector_lake import db_store, wiki_utils


def test_autouse_fixture_never_uses_operator_memory(tmp_path: Path):
    expected = (tmp_path / "MEMORY").resolve()
    live_default = (Path.home() / ".gemini" / "MEMORY").resolve()

    assert wiki_utils.get_memory_dir() == expected
    assert wiki_utils.get_memory_dir() != live_default
    assert db_store.get_db_path().resolve() == (
        expected / "wiki" / ".meta" / "vector_lake.db"
    ).resolve()
