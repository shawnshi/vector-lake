"""VL-A06: physical SQL write amplification and per-assessment count lifetime."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from vector_lake import db_store, page_index_projection as pages, projection_registry as registry


def _seed(memory, count=2000):
    db_store.init_db()
    data = {"nodes": {f"Concept_{i}": {
        "id": f"concept_{i}", "title": f"Node {i}", "type": "Concept",
        "status": "active", "domain": "test", "topic_cluster": "test",
        "summary": "x" * 512, "aliases": [],
    } for i in range(count)}, "weighted_edges": []}
    path = memory / "wiki" / "index.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    pages.refresh_page_index_projection(data)
    return db_store.get_connection(), data, path


def _assert_oracle(conn, data):
    stored = {row["node_key"]: tuple(row) for row in conn.execute(
        "SELECT node_key,node_id,title,type,status,domain,topic_cluster,node_json FROM page_index_nodes"
    )}
    assert stored == {key: pages._node_columns(key, node) for key, node in data["nodes"].items()}


def test_noop_refresh_does_not_rewrite_nodes(isolated_memory, record_property):
    conn, data, _path = _seed(isolated_memory)
    before, started = conn.total_changes, time.perf_counter()
    result = pages.refresh_page_index_projection(data)
    changed = conn.total_changes - before
    record_property("noop_changed_rows_including_state", changed)
    record_property("noop_refresh_ms", round((time.perf_counter() - started) * 1000, 3))
    assert changed == 1, "only the existing page_index_state row should change"
    assert result["nodes_written"] == 2000  # Historical processed-row count is preserved.
    _assert_oracle(conn, data)


def test_edit_one_replaces_one_node_not_the_whole_table(isolated_memory, record_property):
    conn, data, path = _seed(isolated_memory)
    data["nodes"]["Concept_0"]["title"] = "changed"
    path.write_text(json.dumps(data), encoding="utf-8")
    before, started = conn.total_changes, time.perf_counter()
    pages.refresh_page_index_projection(data)
    changed = conn.total_changes - before
    record_property("edit_one_changed_rows_including_state", changed)
    record_property("edit_one_refresh_ms", round((time.perf_counter() - started) * 1000, 3))
    assert changed == 2  # One inserted/replaced node and the state row (implicit REPLACE deletes excluded).
    _assert_oracle(conn, data)


def test_full_refresh_removes_absent_and_repairs_equal_json_columns(isolated_memory):
    conn, data, path = _seed(isolated_memory, count=4)
    with db_store.transaction():
        conn.execute("UPDATE page_index_nodes SET title='corrupt' WHERE node_key='Concept_0'")
    del data["nodes"]["Concept_3"]
    path.write_text(json.dumps(data), encoding="utf-8")
    pages.refresh_page_index_projection(data)
    _assert_oracle(conn, data)
    data["nodes"] = {}
    path.write_text(json.dumps(data), encoding="utf-8")
    pages.refresh_page_index_projection(data)
    _assert_oracle(conn, data)


@pytest.mark.parametrize("partial", [False, True])
def test_null_primary_key_is_removed_by_full_or_partial_fallback(isolated_memory, partial):
    conn, data, _path = _seed(isolated_memory, count=2)
    with db_store.transaction():
        conn.execute("INSERT INTO page_index_nodes(node_key,node_json) VALUES(NULL,'{}')")
    result = pages.refresh_page_index_projection(data, node_keys=["Concept_0"] if partial else None)
    assert result["partial"] is False
    _assert_oracle(conn, data)


def test_successful_status_executes_compound_counts_once(isolated_memory, monkeypatch, record_property):
    db_store.init_db()
    from vector_lake import tantivy_index
    monkeypatch.setattr(tantivy_index, "enabled", lambda: False)
    conn, statements = db_store.get_connection(), []
    conn.set_trace_callback(statements.append)
    try:
        first = registry.status()
        count = sum("AS outbox_lag" in sql for sql in statements)
        record_property("compound_count_queries_per_status", count)
        assert count == 1
        statements.clear()
        second = registry.status()
        assert second == first
        assert sum("AS outbox_lag" in sql for sql in statements) == 1
    finally:
        conn.set_trace_callback(None)


class CountingConnection:
    def __init__(self, value):
        self.value, self.calls = value, 0
    def execute(self, _sql):
        self.calls += 1
        value = self.value
        return SimpleNamespace(fetchone=lambda: {"nodes": value})


def _projection(name, health):
    return registry.Projection(name, "test", "test", health, None, "test")


def test_nested_count_scopes_restore_outer_and_then_discard(monkeypatch):
    conn, seen, depth = CountingConnection(1), [], [0]
    def health():
        seen.append(registry._counts(conn)["nodes"])
        if depth[0] == 0:
            depth[0] = 1
            conn.value = 2
            registry.status()
            conn.value = 3
            seen.append(registry._counts(conn)["nodes"])
        return registry.HEALTHY, "ok"
    monkeypatch.setattr(registry, "registry", lambda: (_projection("test", health),))
    registry.status()
    assert seen == [1, 2, 1]
    assert registry._counts(conn)["nodes"] == 3
    assert conn.calls == 3


def test_failed_status_does_not_leave_a_cached_count(monkeypatch):
    conn = CountingConnection(1)
    def projections():
        yield _projection("first", lambda: (registry.HEALTHY, str(registry._counts(conn))))
        raise RuntimeError("synthetic iterator failure")
    monkeypatch.setattr(registry, "registry", projections)
    with pytest.raises(RuntimeError, match="synthetic iterator"):
        registry.status()
    conn.value = 2
    assert registry._counts(conn)["nodes"] == 2


def test_concurrent_statuses_do_not_share_their_count_scope(monkeypatch):
    barrier, local, reports, errors = threading.Barrier(2), threading.local(), {}, []
    def health():
        first = registry._counts(local.conn)["nodes"]
        barrier.wait(3)
        assert registry._counts(local.conn)["nodes"] == first
        return registry.HEALTHY, str(first)
    monkeypatch.setattr(registry, "registry", lambda: (_projection("test", health),))
    def run(value):
        try:
            local.conn = CountingConnection(value)
            reports[value] = (registry.status(), local.conn.calls)
        except Exception as error:
            errors.append(error)
    workers = [threading.Thread(target=run, args=(value,)) for value in (1, 2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert not errors and not any(worker.is_alive() for worker in workers)
    assert {value: (report["test"]["detail"], calls) for value, (report, calls) in reports.items()} == {1: ("1", 1), 2: ("2", 1)}


def test_count_probe_error_stays_degraded_and_is_not_no_data(isolated_memory):
    db_store.init_db()
    db_store.get_connection().execute("DROP TABLE claim_index")
    report = registry.status()
    assert report["claim_index"]["state"] == registry.DEGRADED
    assert "OperationalError" in report["claim_index"]["detail"]
    assert "no such table" in report["claim_index"]["detail"]
