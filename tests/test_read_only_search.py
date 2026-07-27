import pytest

from vector_lake import db_store, governance_store, tool_search


def test_operational_memory_read_does_not_create_missing_database(isolated_memory):
    meta_path = isolated_memory / "wiki" / ".meta"
    db_path = db_store.peek_db_path()
    assert db_path.exists() is False

    with pytest.raises(
        governance_store.OperationalMemoryNotReady,
        match="database_missing",
    ):
        governance_store.search_operational_memory_views("query")

    assert db_path.exists() is False
    assert meta_path.exists() is False


def test_operational_memory_read_does_not_rebuild_empty_projection(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES ('claim_read_only', 'read only', 'active', '{}', "
            "'2026-07-27T00:00:00+00:00')"
        )

    with pytest.raises(
        governance_store.OperationalMemoryNotReady,
        match="projection_empty",
    ):
        governance_store.search_operational_memory_views("read only")

    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 0


def test_memory_tool_surfaces_projection_maintenance_state(isolated_memory):
    direct = tool_search.format_operational_memory_results("query")
    packet = tool_search.build_memory_packet("query")

    assert "database_missing" in direct
    assert "status='unavailable'" in packet["packet"]
    assert packet["warning_count"] == 1
