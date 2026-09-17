"""Tests for the ``index.json`` -> SQLite read projection.

``index.json`` remains the sovereign artifact; these tests pin the projection to
it, including the edge order that the two-step personalised PageRank walk is
sensitive to.
"""

import json

import pytest

from vector_lake import db_store, page_index_projection
from vector_lake.tool_search import assemble_context, search_vector_lake
from vector_lake.wiki_utils import get_index_path


def _node(node_id: str, title: str, domain: str, cluster: str, links=None) -> dict:
    return {
        "id": node_id,
        "title": title,
        "type": "concept",
        "domain": domain,
        "topic_cluster": cluster,
        "status": "Active",
        "aliases": [],
        "links": links or [],
        "tension_edges": [],
        "sources": [],
        "triples": [],
    }


def _write_index(wiki_dir, nodes, edges) -> None:
    (wiki_dir / "index.json").write_text(
        json.dumps(
            {
                "nodes": nodes,
                "weighted_edges": edges,
                "aliases": {},
                "categories": [],
                "error_log": [],
                "graph_state": {"dirty": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def indexed_memory(isolated_memory):
    db_store.init_db()
    wiki_dir = isolated_memory / "wiki"
    nodes = {
        f"Concept_{index}": _node(f"concept_{index}", f"Page {index}", "Healthcare_IT", "HIS")
        for index in range(5)
    }
    edges = [
        {"source": f"Concept_{index}", "target": f"Concept_{index + 1}", "weight": float(index + 1)}
        for index in range(4)
    ]
    for key in nodes:
        (wiki_dir / f"{key}.md").write_text(f"---\n---\n{key} body", encoding="utf-8")
        db_store.upsert_search_index(key, nodes[key]["title"], "", f"{key} body")
    _write_index(wiki_dir, nodes, edges)
    assert page_index_projection.ensure_page_index_projection() is True
    return isolated_memory


def test_projection_matches_index_json(indexed_memory):
    index = json.loads(get_index_path().read_text(encoding="utf-8"))
    conn = db_store.get_connection()

    projected_nodes = {
        row[0]: json.loads(row[1])
        for row in conn.execute("SELECT node_key, node_json FROM page_index_nodes")
    }
    assert projected_nodes == index["nodes"]

    projected_edges = [
        (int(row[0]), str(row[1]), str(row[2]), float(row[3]))
        for row in conn.execute(
            "SELECT sequence, source_key, target_key, weight FROM page_index_edges ORDER BY sequence"
        )
    ]
    assert projected_edges == [
        (position, edge["source"], edge["target"], float(edge["weight"]))
        for position, edge in enumerate(index["weighted_edges"])
    ]


def test_adjacency_matches_the_previous_in_code_build(indexed_memory):
    index = json.loads(get_index_path().read_text(encoding="utf-8"))
    reference: dict[str, list[tuple[str, float]]] = {}
    for edge in index["weighted_edges"]:
        source, target, weight = edge["source"], edge["target"], edge.get("weight", 1.0)
        reference.setdefault(source, []).append((target, weight))
        reference.setdefault(target, []).append((source, weight))

    # Same keys, same order, same values -- the walk is order-sensitive.
    assert list(page_index_projection.adjacency().items()) == list(reference.items())


def test_nodes_are_fetched_per_key_and_preserve_request_order(indexed_memory):
    found = page_index_projection.nodes_by_key(["Concept_3", "Concept_1", "missing"])

    assert list(found) == ["Concept_3", "Concept_1"]
    assert found["Concept_3"]["title"] == "Page 3"


def test_stale_index_json_is_reprojected(indexed_memory):
    wiki_dir = indexed_memory / "wiki"
    index = json.loads(get_index_path().read_text(encoding="utf-8"))
    index["nodes"]["Concept_9"] = _node("concept_9", "Page 9", "Healthcare_IT", "HIS")
    index["weighted_edges"].append({"source": "Concept_9", "target": "Concept_0", "weight": 2.0})

    # A different size guarantees a different (mtime, size) stamp.
    _write_index(wiki_dir, index["nodes"], index["weighted_edges"])

    assert page_index_projection.ensure_page_index_projection() is True
    assert "Concept_9" in page_index_projection.nodes_by_key(["Concept_9"])
    assert page_index_projection.projection_state()["node_count"] == 6
    assert page_index_projection.projection_state()["edge_count"] == 5


def test_partial_refresh_drops_removed_nodes(indexed_memory):
    index = json.loads(get_index_path().read_text(encoding="utf-8"))
    del index["nodes"]["Concept_4"]
    (indexed_memory / "wiki" / "Concept_4.md").unlink()

    result = page_index_projection.refresh_page_index_projection(index, node_keys=["Concept_0"])

    assert result["partial"] is False, "a node set change must fall back to a full refresh"
    assert page_index_projection.nodes_by_key(["Concept_4"]) == {}


def test_partial_refresh_keeps_unrelated_nodes(indexed_memory):
    index = json.loads(get_index_path().read_text(encoding="utf-8"))
    index["nodes"]["Concept_2"] = _node("concept_2", "Renamed page", "Healthcare_IT", "HIS")

    result = page_index_projection.refresh_page_index_projection(index, node_keys=["Concept_2"])

    assert result["partial"] is True
    assert result["nodes_written"] == 1
    assert page_index_projection.nodes_by_key(["Concept_2"])["Concept_2"]["title"] == "Renamed page"
    assert len(page_index_projection.nodes_by_key(["Concept_0", "Concept_1", "Concept_3"])) == 3


def test_search_and_context_read_the_projection(indexed_memory):
    result = search_vector_lake("Page 1", top_k=3)
    assert "Page 1" in result

    context = assemble_context("Page 1")
    assert "Page" in context["index_summary"]
    assert context["budget_used"] <= context["budget_max"]


def test_missing_index_is_reported_as_drying(isolated_memory):
    db_store.init_db()

    assert page_index_projection.ensure_page_index_projection() is False
    result = search_vector_lake("anything", top_k=3)

    assert "drying" in result.lower()


# --- incremental edge projection -------------------------------------------
#
# The state stamp is ``(mtime, size)``, so any ``index.json`` rewrite used to
# re-project every edge row under the write lock even when the topology was
# untouched.  These pin the content check that lets the rewrite be skipped.


def _reindex_payload(wiki_dir) -> dict:
    return json.loads((wiki_dir / "index.json").read_text(encoding="utf-8"))


def test_unchanged_edges_skip_the_edge_rewrite(indexed_memory):
    wiki_dir = indexed_memory / "wiki"
    payload = _reindex_payload(wiki_dir)

    result = page_index_projection.refresh_page_index_projection(payload)

    assert result["edges_rewritten"] is False
    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0] == 4
    assert page_index_projection.projection_state()["edge_count"] == 4


def test_changed_edge_weight_forces_the_rewrite(indexed_memory):
    wiki_dir = indexed_memory / "wiki"
    payload = _reindex_payload(wiki_dir)
    payload["weighted_edges"][0]["weight"] = 99.0

    result = page_index_projection.refresh_page_index_projection(payload)

    assert result["edges_rewritten"] is True
    conn = db_store.get_connection()
    row = conn.execute("SELECT weight FROM page_index_edges WHERE sequence = 0").fetchone()
    assert float(row[0]) == 99.0


def test_edge_count_change_is_detected_without_hashing(indexed_memory):
    wiki_dir = indexed_memory / "wiki"
    payload = _reindex_payload(wiki_dir)
    payload["weighted_edges"] = payload["weighted_edges"][:3]

    result = page_index_projection.refresh_page_index_projection(payload)

    assert result["edges_rewritten"] is True
    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0] == 3


def test_recorded_digest_matches_the_projected_edges(indexed_memory):
    wiki_dir = indexed_memory / "wiki"
    payload = _reindex_payload(wiki_dir)

    page_index_projection.refresh_page_index_projection(payload)

    state = page_index_projection.projection_state()
    assert state["edge_digest"] == page_index_projection._weighted_edges_digest(payload)


def test_skipped_rewrite_leaves_the_adjacency_consistent(indexed_memory):
    wiki_dir = indexed_memory / "wiki"
    payload = _reindex_payload(wiki_dir)
    before = page_index_projection.adjacency()

    page_index_projection.refresh_page_index_projection(payload)

    assert page_index_projection.adjacency() == before


def test_refresh_without_an_edge_key_keeps_the_projection(indexed_memory):
    """A payload that never claims edges must not erase them."""
    wiki_dir = indexed_memory / "wiki"
    payload = _reindex_payload(wiki_dir)
    payload.pop("weighted_edges")

    result = page_index_projection.refresh_page_index_projection(payload)

    assert result["edges_rewritten"] is False
    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0] == 4
