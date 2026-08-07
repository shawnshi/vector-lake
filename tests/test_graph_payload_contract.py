from pathlib import Path

from vector_lake import tool_graph


ROOT = Path(__file__).resolve().parents[1]


def test_graph_payload_serializes_page_graph_once():
    index_data = {
        "nodes": {
            "Concept_One": {
                "title": "One",
                "categories": ["Concept"],
                "links": ["Concept_Two"],
            },
            "Concept_Two": {
                "title": "Two",
                "categories": ["Concept"],
                "links": [],
            },
        },
        "weighted_edges": [
            {"source": "Concept_One", "target": "Concept_Two", "weight": 1.0}
        ],
        "community_labels": {"0": "Core"},
        "governance_metrics": {"open_items": 3},
    }
    claim_graph = {
        "nodes": [{"id": "claim_one", "name": "Claim One"}],
        "edges": [],
    }

    payload = tool_graph._build_graph_payload(index_data, claim_graph)

    assert set(payload) == {"pageGraph", "claimGraph", "governanceMetrics"}
    assert len(payload["pageGraph"]["nodes"]) == 2
    assert payload["pageGraph"]["edges"] == index_data["weighted_edges"]
    assert payload["claimGraph"]["nodes"][0]["id"] == "claim_one"
    assert payload["governanceMetrics"] == {"open_items": 3}


def test_topology_template_has_no_legacy_duplicate_page_graph_fallback():
    content = (ROOT / "templates" / "topology.html").read_text(encoding="utf-8")

    assert "rawData.pageGraph" in content
    assert "rawData.claimGraph" in content
    assert "rawData.nodes" not in content
    assert "rawData.edges" not in content
    assert "rawData.community_labels" not in content
