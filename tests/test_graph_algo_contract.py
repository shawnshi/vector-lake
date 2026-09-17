"""Contract tests for the visualisation graph algorithms.

Each test here pins one invariant that the graph pipeline had silently broken
in the live index, so the regression is observable rather than a comment:

1. ``weighted_edges`` carries no duplicate unordered pairs and no node above
   ``MAX_EDGES_PER_NODE`` -- the partial-update path used to skip both rules
   (live index: 1,607,843 rows, mean degree 487.7, 314,660 duplicate pairs).
2. Backbone selection keeps every connected node attached while the budget
   stretches (5,000 edges over 7,125 pages left 909 orphans).
3. ``_stabilize_community_ids`` returns a partition, not a merge.
4. The rendered payload reports the true undirected degree, keeps the node
   arrays in one place only, and surfaces community staleness.
5. ``communities`` values are opaque string tokens (the frontend hashes them).
"""

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

from vector_lake import indexer, tool_graph  # noqa: E402


def _load_daemon():
    """Import the clustering daemon without executing its main entry point."""
    spec = importlib.util.spec_from_file_location(
        "_vl_clustering_daemon_contract", ROOT / "scripts" / "community_clustering_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_vl_clustering_daemon_contract"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Edge set contract
# --------------------------------------------------------------------------


def test_dedupe_and_prune_edges_caps_every_node():
    edges = [
        {"source": "Hub", "target": f"Leaf_{i}", "weight": float(100 - i)}
        for i in range(50)
    ]

    pruned = indexer.dedupe_and_prune_edges(edges)

    assert len(pruned) == indexer.MAX_EDGES_PER_NODE
    degree = {}
    for edge in pruned:
        degree[edge["source"]] = degree.get(edge["source"], 0) + 1
        degree[edge["target"]] = degree.get(edge["target"], 0) + 1
    assert degree["Hub"] == indexer.MAX_EDGES_PER_NODE
    # Highest weights win.
    assert pruned[0]["weight"] == 100.0


def test_dedupe_and_prune_edges_collapses_reversed_pairs_and_keeps_max_weight():
    edges = [
        {"source": "B", "target": "A", "weight": 2.0},
        {"source": "A", "target": "B", "weight": 9.0},
        {"source": "A", "target": "B", "weight": 4.0},
    ]

    pruned = indexer.dedupe_and_prune_edges(edges)

    assert pruned == [{"source": "A", "target": "B", "weight": 9.0}]


def test_dedupe_and_prune_edges_is_deterministic():
    edges = [
        {"source": "B", "target": "C", "weight": 5.0},
        {"source": "A", "target": "B", "weight": 5.0},
        {"source": "A", "target": "C", "weight": 5.0},
    ]

    first = indexer.dedupe_and_prune_edges(edges)
    second = indexer.dedupe_and_prune_edges(list(reversed(edges)))

    assert first == second


def test_duplicate_rows_do_not_consume_the_backbone_budget():
    valid_nodes = {f"Node_{i}" for i in range(6)}
    raw_edges = []
    for i in range(5):
        # Every row is present three times, which previously consumed quota.
        for _ in range(3):
            raw_edges.append(
                {"source": f"Node_{i}", "target": f"Node_{i + 1}", "weight": float(i + 1)}
            )

    edges, degrees = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges, valid_node_ids=valid_nodes, max_edges=10, min_weight=0.0
    )

    assert len(edges) == 5
    assert len({(e["source"], e["target"]) for e in edges}) == 5
    assert set(degrees) == valid_nodes


# --------------------------------------------------------------------------
# 2. Backbone coverage
# --------------------------------------------------------------------------


def test_backbone_keeps_every_connected_node_attached():
    # A tree over 40 nodes plus weak cross links: with a budget of 20 edges a
    # pairing pass can cover all 40 nodes, which weight-ordered greedy alone
    # did not do (it burned each slot on one new node).
    nodes = {f"N{i}" for i in range(40)}
    raw_edges = [{"source": f"N{i}", "target": f"N{i + 1}", "weight": float(i + 1)} for i in range(39)]
    raw_edges += [
        {"source": f"N{i}", "target": f"N{i + 2}", "weight": float(i) / 100.0} for i in range(38)
    ]

    edges, degrees = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges, valid_node_ids=nodes, max_edges=20, min_weight=0.0
    )

    assert len(edges) == 20
    assert set(degrees) == nodes, "every connected node must keep at least one edge"


def test_backbone_still_fills_the_budget_when_coverage_is_cheap():
    # A perfect matching over 10 nodes costs 5 edges; the rest must be spent on
    # weight salience rather than left unused.
    nodes = {f"N{i}" for i in range(10)}
    raw_edges = [{"source": f"N{i}", "target": f"N{i + 1}", "weight": float(i + 1)} for i in range(9)]

    edges, _ = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges, valid_node_ids=nodes, max_edges=8, min_weight=0.0
    )

    assert len(edges) == 8


def test_backbone_relation_labels_are_classified_by_weight():
    nodes = {"A", "B", "C", "D"}
    raw_edges = [
        {"source": "A", "target": "B", "weight": 12.0},
        {"source": "B", "target": "C", "weight": 6.0},
        {"source": "C", "target": "D", "weight": 1.0},
    ]

    edges, _ = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges, valid_node_ids=nodes, max_edges=10, min_weight=0.0
    )

    labels = {(e["source"], e["target"]): e["relation"] for e in edges}
    assert labels[("A", "B")] == "strong_association"
    assert labels[("B", "C")] == "association"
    assert labels[("C", "D")] == "related_to"


# --------------------------------------------------------------------------
# 3. Community id stabilisation
# --------------------------------------------------------------------------


def test_stabilize_community_ids_never_shares_an_id():
    daemon = _load_daemon()
    # Five new communities splitting three old ones: the old code let the
    # second and third new community reuse an already-claimed old id.
    old_partition = {
        "a": "u1", "b": "u1", "c": "u1",
        "d": "u2", "e": "u2", "f": "u2",
        "g": "u3", "h": "u3",
    }
    new_partition = {"a": 0, "b": 1, "c": 1, "d": 1, "e": 2, "f": 2, "g": 3, "h": 4}

    partition, diffs, stable_uuids = daemon._stabilize_community_ids(
        new_partition, old_partition
    )

    assert len(set(stable_uuids.values())) == len(stable_uuids)
    # The result must still be a partition: distinct new communities stay
    # distinct, one stable id per cluster.
    assert len(set(partition.values())) == len(set(new_partition.values()))
    assert set(partition) == set(new_partition)
    assert len(diffs) == len(stable_uuids)


def test_stabilize_community_ids_reuses_ids_when_overlap_is_total():
    daemon = _load_daemon()
    old_partition = {"a": "u1", "b": "u1", "c": "u2", "d": "u2"}
    new_partition = {"a": 0, "b": 0, "c": 1, "d": 1}

    partition, _, _ = daemon._stabilize_community_ids(new_partition, old_partition)

    assert partition == {"a": "u1", "b": "u1", "c": "u2", "d": "u2"}


def test_stabilize_community_ids_matches_the_larger_overlap():
    daemon = _load_daemon()
    # u1 loses its majority: two of its nodes moved into the community that
    # takes over its id, so the id must follow the bigger share.
    old_partition = {"a": "u1", "b": "u1", "c": "u1", "d": "u2"}
    new_partition = {"a": 0, "b": 0, "c": 0, "d": 1}

    partition, _, stable_uuids = daemon._stabilize_community_ids(new_partition, old_partition)

    assert partition["a"] == partition["b"] == partition["c"]
    assert partition["a"] != partition["d"]
    assert len(set(stable_uuids.values())) == 2


# --------------------------------------------------------------------------
# 4. Rendering payload
# --------------------------------------------------------------------------


def _index_fixture():
    return {
        "nodes": {
            "Concept_A": {"id": "n1", "title": "A", "categories": ["Concept"], "links": ["Concept_B"]},
            "Concept_B": {"id": "n2", "title": "B", "categories": ["Concept"], "links": []},
            "Concept_C": {"id": "n3", "title": "C", "categories": ["Concept"], "links": []},
        },
        "communities": {"Concept_A": "6fd2edf4", "Concept_B": "6fd2edf4", "Concept_C": "084d10f5"},
        "community_labels": {"6fd2edf4": "L0 Comm: A / B", "084d10f5": "L0 Comm: C"},
        "weighted_edges": [
            {"source": "Concept_A", "target": "Concept_B", "weight": 12.0},
            {"source": "Concept_A", "target": "Concept_B", "weight": 12.0},  # duplicate row
            {"source": "Concept_B", "target": "Concept_C", "weight": 4.0},
        ],
        "graph_state": {"dirty": False, "clustering_stale": True},
        "governance_metrics": {},
    }


def test_payload_reports_true_undirected_degree_and_string_community():
    payload = tool_graph._build_graph_payload(index_data=_index_fixture(), claim_graph_data={})

    nodes = {node["id"]: node for node in payload["pageGraph"]["nodes"]}
    # A has one unique edge (to B); the duplicated row must not inflate it.
    assert nodes["Concept_A"]["degree"] == 1
    assert nodes["Concept_B"]["degree"] == 2
    assert nodes["Concept_C"]["degree"] == 1
    assert all(isinstance(node["community"], str) for node in payload["pageGraph"]["nodes"])
    assert nodes["Concept_C"]["community"] == "084d10f5"


def test_payload_keeps_node_arrays_in_one_place():
    payload = tool_graph._build_graph_payload(index_data=_index_fixture(), claim_graph_data={})

    assert "nodes" not in payload
    assert "edges" not in payload
    assert "community_labels" not in payload
    assert payload["pageGraph"]["nodes"]
    assert payload["pageGraph"]["community_labels"]


def test_payload_surfaces_community_staleness_and_read_consistency():
    payload = tool_graph._build_graph_payload(
        index_data=_index_fixture(), claim_graph_data={}, read_consistency="read_only_fallback"
    )

    assert payload["meta"]["communities_stale"] is True
    assert payload["meta"]["read_consistency"] == "read_only_fallback"

    clean = _index_fixture()
    clean["graph_state"] = {"dirty": False, "clustering_stale": False}
    payload = tool_graph._build_graph_payload(index_data=clean, claim_graph_data={})
    assert payload["meta"]["communities_stale"] is False
    assert payload["meta"]["read_consistency"] == "locked"


def test_payload_counts_orphans_from_the_backbone():
    payload = tool_graph._build_graph_payload(index_data=_index_fixture(), claim_graph_data={})

    assert payload["meta"]["orphan_page_nodes"] == 0


def test_payload_json_round_trip_has_no_duplicated_node_objects():
    payload = tool_graph._build_graph_payload(index_data=_index_fixture(), claim_graph_data={})

    serialized = json.dumps(payload, ensure_ascii=False)
    assert serialized.count('"node_kind": "page"') == len(payload["pageGraph"]["nodes"])


def test_clustering_staleness_falls_back_to_dirty_for_legacy_payloads():
    assert indexer.is_clustering_stale({"graph_state": {"dirty": True}}) is True
    assert indexer.is_clustering_stale({"graph_state": {"dirty": False}}) is False
    assert indexer.is_clustering_stale({"graph_state": {"dirty": False, "clustering_stale": True}}) is True
    assert indexer.is_clustering_stale(None) is True


# --------------------------------------------------------------------------
# 5. Graph insights producer
# --------------------------------------------------------------------------


def test_graph_insights_emit_the_consumer_contract():
    daemon = _load_daemon()
    nodes = ["A", "B", "C", "D", "E", "X", "Y", "Z", "Isolated"]
    edges = [
        {"source": "A", "target": "B", "weight": 1.0},
        {"source": "B", "target": "C", "weight": 1.0},
        {"source": "D", "target": "E", "weight": 1.0},
    ]
    communities = {
        "A": "c1", "B": "c1", "C": "c1",
        "D": "c2", "E": "c2",
        "X": "c3", "Y": "c3", "Z": "c3",
        "Isolated": "c4",
    }

    insights = daemon._build_graph_insights(nodes, edges, communities)

    types = {insight["type"] for insight in insights}
    assert "isolated_node" in types
    assert "sparse_community" in types

    isolated = [i for i in insights if i["type"] == "isolated_node"]
    # tool_research reads insight["node"]; audit_graph maps it to a wiki path.
    # X/Y/Z carry no edge at all, so they are reported as isolated *and* as
    # members of the sparse community they are grouped into.
    assert [i["node"] for i in isolated] == ["Isolated", "X", "Y", "Z"]

    sparse = [i for i in insights if i["type"] == "sparse_community"]
    # c1 has two internal edges over three members and c2 one over two: neither
    # is sparse.  c3 is a label with no internal topology at all.
    assert [i["community_id"] for i in sparse] == ["c3"]
    assert sparse[0]["nodes"] == ["X", "Y", "Z"]


def test_graph_insights_are_deterministic_and_capped():
    daemon = _load_daemon()
    nodes = [f"N{i}" for i in range(50)]
    communities = {f"N{i}": f"c{i}" for i in range(50)}

    first = daemon._build_graph_insights(nodes, [], communities)
    second = daemon._build_graph_insights(list(reversed(nodes)), [], communities)

    assert first == second
    assert len(first) == 20


# --------------------------------------------------------------------------
# 6. Default output location
# --------------------------------------------------------------------------


def test_default_output_path_is_the_memory_scratch_tree(tmp_path):
    memory_dir = tmp_path / "MEMORY"
    memory_dir.mkdir()

    path = tool_graph._graph_output_path(str(memory_dir))

    assert path == str(memory_dir / "scratch" / "vector_lake_graph.html")
    assert (memory_dir / "scratch").is_dir()


def test_default_output_path_never_escapes_the_memory_root(tmp_path):
    memory_dir = tmp_path / "MEMORY"
    memory_dir.mkdir()

    path = pathlib.Path(tool_graph._graph_output_path(str(memory_dir)))

    assert path.is_relative_to(memory_dir)
    # The previous default was <parent-of-memory_dir>/tmp, one level above the
    # memory root, so a default call wrote a generated artifact into the host's
    # home layout instead of the lake's own scratch zone.
    assert not path.is_relative_to(tmp_path / "tmp")
    assert path.parent.parent == memory_dir
