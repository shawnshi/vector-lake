import json
import sqlite3

import pytest

from vector_lake.tool_legacy_graph_audit import audit_legacy_graph_connection


def _connection(*, reverse_insert_order: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE wiki_nodes (
            node_key TEXT PRIMARY KEY,
            title TEXT,
            type TEXT,
            domain TEXT,
            topic_cluster TEXT,
            status TEXT,
            metadata_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE wiki_edges (
            source TEXT,
            target TEXT,
            weight REAL,
            PRIMARY KEY (source, target)
        );
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            canonical_name TEXT,
            data_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE claim_graph_nodes (
            node_id TEXT PRIMARY KEY,
            data_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE claim_graph_edges (
            source_id TEXT,
            target_id TEXT,
            relation TEXT,
            weight REAL,
            updated_at TEXT,
            PRIMARY KEY (source_id, target_id, relation)
        );
        CREATE TABLE page_graph_edges (
            source_id TEXT,
            target_id TEXT,
            relation TEXT,
            weight REAL,
            updated_at TEXT,
            PRIMARY KEY (source_id, target_id, relation)
        );
        """
    )
    payloads = [
        {
            "page_key": "Concept_A",
            "title": "A",
            "type": "concept",
            "domain": "Health",
            "topic_cluster": "Graph",
            "status": "Active",
        },
        {
            "page_key": "Concept_B",
            "title": "B",
            "type": "concept",
            "domain": "Health",
            "topic_cluster": "Graph",
            "status": "Active",
        },
    ]
    if reverse_insert_order:
        payloads.reverse()
    for payload in payloads:
        raw = json.dumps(payload, ensure_ascii=False)
        conn.execute(
            "INSERT INTO wiki_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload["page_key"],
                payload["title"],
                payload["type"],
                payload["domain"],
                payload["topic_cluster"],
                payload["status"],
                raw,
                "2026-08-02T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO entities VALUES (?, ?, ?, ?)",
            (
                "entity_" + payload["title"].lower(),
                payload["title"],
                raw,
                "2026-08-02T00:00:00+00:00",
            ),
        )
    conn.execute(
        "INSERT INTO wiki_edges VALUES ('Concept_A', 'Concept_B', 2.5)"
    )
    for table_name in ("claim_graph_edges", "page_graph_edges"):
        conn.execute(
            f"INSERT INTO {table_name} VALUES "
            "('Concept_B', 'Concept_A', 'related_to', 2.5, "
            "'2026-08-02T00:00:00+00:00')"
        )
    conn.commit()
    return conn


def test_nonempty_weighted_graph_is_never_treated_as_relation_graph_equivalent():
    conn = _connection()
    before_changes = conn.total_changes
    conn.execute("PRAGMA query_only=ON")

    report = audit_legacy_graph_connection(conn, sample_limit=1)

    assert report["read_only"] is True
    assert report["caller_owned_connection"] is True
    assert report["tables"]["wiki_nodes"]["row_count"] == 2
    assert report["tables"]["wiki_edges"]["row_count"] == 1
    assert report["node_coverage"]["legacy_keys"] == {
        "count": 2,
        "stable_hash": report["node_coverage"]["legacy_keys"]["stable_hash"],
        "sample": ["Concept_A"],
    }
    assert report["legacy_edge_endpoints"]["all_keys"]["count"] == 2
    assert report["legacy_edge_endpoints"]["all_keys"]["sample"] == ["Concept_A"]
    assert report["relation_graph_pair_overlap"]["overlap_pairs"]["sample"] == [
        ["Concept_A", "Concept_B"]
    ]
    assert report["relation_graph_pair_overlap"]["directed_overlap_pairs"][
        "count"
    ] == 0
    assert report["semantic_equivalence"]["graph_models"] is False
    assert "graph_semantics_not_equivalent" in report["deletion_blockers"]
    assert report["deletion_ready"] is False
    assert conn.total_changes == before_changes
    assert conn.execute("SELECT COUNT(*) FROM wiki_nodes").fetchone()[0] == 2


def test_empty_legacy_edge_table_can_be_ready_when_nodes_and_current_graph_are_sound():
    conn = _connection()
    conn.execute("DELETE FROM wiki_edges")
    conn.commit()

    report = audit_legacy_graph_connection(conn)

    assert report["semantic_equivalence"]["nodes"] is True
    assert report["semantic_equivalence"]["graph_models"] is True
    assert report["deletion_blockers"] == []
    assert report["deletion_ready"] is True


def test_hashes_and_fingerprint_ignore_insert_order_row_factory_and_sample_limit():
    left = _connection()
    right = _connection(reverse_insert_order=True)
    right.row_factory = sqlite3.Row

    left_report = audit_legacy_graph_connection(left, sample_limit=0)
    right_report = audit_legacy_graph_connection(right, sample_limit=1)

    for table_name in (
        "wiki_nodes",
        "wiki_edges",
        "entities",
        "claim_graph_nodes",
        "claim_graph_edges",
        "page_graph_edges",
    ):
        assert (
            left_report["tables"][table_name]["stable_hash"]
            == right_report["tables"][table_name]["stable_hash"]
        )
    assert left_report["baseline_fingerprint"] == right_report["baseline_fingerprint"]
    assert left_report["node_coverage"]["legacy_keys"]["sample"] == []
    assert len(right_report["node_coverage"]["legacy_keys"]["sample"]) == 1


def test_temp_table_shadow_cannot_hide_main_legacy_rows():
    conn = _connection()
    conn.execute(
        "CREATE TEMP TABLE wiki_edges (source TEXT, target TEXT, weight REAL)"
    )

    report = audit_legacy_graph_connection(conn)

    assert report["tables"]["wiki_edges"]["row_count"] == 1
    assert report["relation_graph_pair_overlap"]["legacy_pairs"]["count"] == 1
    assert "graph_semantics_not_equivalent" in report["deletion_blockers"]
    assert report["deletion_ready"] is False


def test_external_consumer_finding_is_an_independent_hard_blocker():
    conn = _connection()
    conn.execute("DELETE FROM wiki_edges")
    conn.commit()

    report = audit_legacy_graph_connection(
        conn,
        external_consumer_findings=[
            {
                "path": "C:/legacy/vector_lake/indexer.py",
                "consumer": "wiki_nodes",
            }
        ],
    )

    assert report["semantic_equivalence"]["overall"] is True
    assert report["external_consumer_findings"]["count"] == 1
    assert report["external_consumer_findings"]["sample"] == [
        {
            "consumer": "wiki_nodes",
            "path": "C:/legacy/vector_lake/indexer.py",
        }
    ]
    assert "external_consumers_detected:1" in report["deletion_blockers"]
    assert report["deletion_ready"] is False


def test_node_and_edge_gaps_fail_closed_with_bounded_diff_evidence():
    conn = _connection()
    conn.execute("DELETE FROM entities WHERE entity_id = 'entity_b'")
    conn.execute("DELETE FROM claim_graph_edges")
    conn.execute("DELETE FROM page_graph_edges")
    conn.commit()

    report = audit_legacy_graph_connection(conn)

    assert report["node_coverage"]["legacy_only_keys"]["sample"] == ["Concept_B"]
    assert report["legacy_edge_endpoints"]["missing_from_canonical_page_keys"][
        "sample"
    ] == ["Concept_B"]
    assert report["relation_graph_pair_overlap"]["legacy_only_pairs"]["sample"] == [
        ["Concept_A", "Concept_B"]
    ]
    assert "legacy_node_coverage_gap:1" in report["deletion_blockers"]
    assert "legacy_relation_pair_gap:1" in report["deletion_blockers"]
    assert report["deletion_ready"] is False


def test_payload_weight_and_dual_write_mismatches_are_semantic_blockers():
    conn = _connection()
    payload = json.loads(
        conn.execute(
            "SELECT data_json FROM entities WHERE entity_id = 'entity_a'"
        ).fetchone()[0]
    )
    payload["title"] = "changed"
    conn.execute(
        "UPDATE entities SET data_json = ? WHERE entity_id = 'entity_a'",
        (json.dumps(payload, ensure_ascii=False),),
    )
    conn.execute(
        "UPDATE claim_graph_edges SET weight = 3.0 WHERE source_id = 'Concept_B'"
    )
    conn.commit()

    report = audit_legacy_graph_connection(conn)

    assert report["node_coverage"]["payload_mismatch_keys"]["sample"] == [
        "Concept_A"
    ]
    assert report["relation_graph_pair_overlap"]["weight_mismatch_pairs"][
        "sample"
    ] == [["Concept_A", "Concept_B"]]
    assert report["claim_page_relation_diff"]["claim_only_semantic_rows"]["count"]
    assert report["claim_page_relation_diff"]["page_only_semantic_rows"]["count"]
    assert "legacy_node_payload_mismatch:1" in report["deletion_blockers"]
    assert "legacy_relation_weight_mismatch:1" in report["deletion_blockers"]
    assert "current_relation_graph_dual_write_divergence" in report[
        "deletion_blockers"
    ]
    assert report["deletion_ready"] is False


def test_missing_current_table_and_invalid_json_never_authorize_deletion():
    conn = _connection()
    conn.execute("DROP TABLE claim_graph_nodes")
    conn.execute(
        "UPDATE wiki_nodes SET metadata_json = '{bad json' "
        "WHERE node_key = 'Concept_A'"
    )
    conn.commit()

    report = audit_legacy_graph_connection(conn)

    assert "current_table_missing:claim_graph_nodes" in report["schema_issues"]
    assert report["node_coverage"]["invalid_legacy_metadata"]["sample"] == [
        "Concept_A"
    ]
    assert report["deletion_ready"] is False


@pytest.mark.parametrize("sample_limit", [-1, 101])
def test_sample_limit_is_bounded(sample_limit):
    conn = _connection()

    with pytest.raises(ValueError, match="between 0 and 100"):
        audit_legacy_graph_connection(conn, sample_limit=sample_limit)


def test_sample_limit_rejects_non_integer_values():
    conn = _connection()

    with pytest.raises(TypeError, match="must be an integer"):
        audit_legacy_graph_connection(conn, sample_limit=True)
