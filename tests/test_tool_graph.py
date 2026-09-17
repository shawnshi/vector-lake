import pytest
from vector_lake import tool_graph


def test_extract_backbone_edges_filters_invalid_and_self_loops():
    valid_nodes = {"nodeA", "nodeB", "nodeC"}
    raw_edges = [
        {"source": "nodeA", "target": "nodeB", "weight": 5.0},
        {"source": "nodeA", "target": "nodeA", "weight": 10.0},  # self loop
        {"source": "nodeA", "target": "danglingNode", "weight": 20.0},  # invalid target
        {"source": "unknownSource", "target": "nodeB", "weight": 15.0},  # invalid source
        {"source": "nodeB", "target": "nodeC", "weight": 1.0},
    ]

    edges, degrees = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges,
        valid_node_ids=valid_nodes,
        max_edges=10,
        min_weight=0.0,
    )

    assert len(edges) == 2
    sources_targets = {(e["source"], e["target"]) for e in edges}
    assert ("nodeA", "nodeB") in sources_targets
    assert ("nodeB", "nodeC") in sources_targets
    assert degrees["nodeA"] == 1
    assert degrees["nodeB"] == 2
    assert degrees["nodeC"] == 1


def test_extract_backbone_edges_enforces_max_edges_and_retains_connectivity():
    valid_nodes = {f"node_{i}" for i in range(20)}
    raw_edges = []
    # Create 50 edges with varying weights
    for i in range(19):
        raw_edges.append({"source": f"node_{i}", "target": f"node_{i+1}", "weight": float(i + 1)})
    for i in range(30):
        raw_edges.append({"source": f"node_{i%10}", "target": f"node_{(i+5)%20}", "weight": float(i * 2)})

    max_limit = 10
    edges, degrees = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges,
        valid_node_ids=valid_nodes,
        max_edges=max_limit,
        min_weight=0.0,
    )

    assert len(edges) == max_limit
    assert all(e["weight"] > 0 for e in edges)
    # Check relations are populated
    assert all("relation" in e for e in edges)


def test_build_page_graph_structure():
    index_data = {
        "nodes": {
            "Concept_AI": {"id": "nid1", "title": "AI", "categories": ["Concept"], "links": ["Product_Bot"]},
            "Product_Bot": {"id": "nid2", "title": "Bot", "categories": ["Product"], "links": []},
            "Orphan_Node": {"id": "nid3", "title": "Orphan", "categories": ["Other"], "links": []},
        },
        "communities": {"Concept_AI": 1, "Product_Bot": 1, "Orphan_Node": 2},
        "weighted_edges": [
            {"source": "Concept_AI", "target": "Product_Bot", "weight": 12.5},
        ],
        "community_labels": {"1": "AI Cluster", "2": "Other Cluster"},
    }

    page_graph = tool_graph._build_page_graph(index_data, max_edges=10)
    assert len(page_graph["nodes"]) == 3
    assert len(page_graph["edges"]) == 1

    node_map = {n["id"]: n for n in page_graph["nodes"]}
    assert node_map["Concept_AI"]["degree"] == 1
    assert node_map["Concept_AI"]["backbone_degree"] == 1
    assert not node_map["Concept_AI"]["is_orphan"]

    assert node_map["Orphan_Node"]["is_orphan"]
    assert node_map["Orphan_Node"]["backbone_degree"] == 0


def test_build_graph_payload_meta_auditing():
    index_data = {
        "nodes": {f"Page_{i}": {"title": f"P{i}", "categories": ["Page"]} for i in range(10)},
        "communities": {},
        "weighted_edges": [
            {"source": f"Page_{i}", "target": f"Page_{i+1}", "weight": float(i + 1)}
            for i in range(9)
        ],
        "governance_metrics": {"validity_state_counts": {"active": 5}},
    }
    claim_data = {
        "nodes": [{"id": "claim_1", "name": "C1"}],
        "edges": [],
    }

    payload = tool_graph._build_graph_payload(
        index_data=index_data,
        claim_graph_data=claim_data,
        max_page_edges=5,  # limit to 5 while 9 exist
    )

    assert "meta" in payload
    meta = payload["meta"]
    assert meta["total_page_nodes"] == 10
    assert meta["total_page_edges"] == 9
    assert meta["rendered_page_edges"] == 5
    assert meta["pruned"] is True
    assert len(payload["pageGraph"]["edges"]) == 5
