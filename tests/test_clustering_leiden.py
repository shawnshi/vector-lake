"""Regression tests for the Louvain -> Leiden community detection migration.

python-louvain is no longer a dependency; clustering is Leiden via
leidenalg + igraph (see scripts/community_clustering_daemon.py).
"""
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_daemon():
    """"Import the clustering daemon without executing its main entry point."""
    spec = importlib.util.spec_from_file_location(
        "_vl_clustering_daemon", ROOT / "scripts" / "community_clustering_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_vl_clustering_daemon"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment problem
        pytest.skip(f"clustering daemon needs leidenalg+igraph: {exc}")
    return module


pytest.importorskip("igraph")
pytest.importorskip("leidenalg")

DAEMON = _load_daemon()


def _three_cluster_graph(clusters=3, per_cluster=6, intra=6.0, inter=1.6):
    nodes = []
    edges = []
    for cluster in range(clusters):
        members = [f"C{cluster}_N{index}" for index in range(per_cluster)]
        nodes.extend(members)
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                edges.append({"source": left, "target": right, "weight": intra})
        if cluster:
            edges.append({"source": members[0], "target": "C0_N0", "weight": inter})
    return nodes, edges


def _same_cluster_pairs(partition, clusters=3, per_cluster=6):
    checked = joined = 0
    for cluster in range(clusters):
        members = [f"C{cluster}_N{index}" for index in range(per_cluster)]
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                checked += 1
                if partition.get(left) == partition.get(right):
                    joined += 1
        assert checked
    return joined / checked


def test_leiden_recovers_planted_clusters():
    nodes, edges = _three_cluster_graph()

    partition = DAEMON._leiden_partition(nodes, edges, 1.0)

    assert set(partition) == set(nodes), "every node must be assigned a community"
    assert len(set(partition.values())) == 3
    assert _same_cluster_pairs(partition) == 1.0


def test_higher_resolution_is_not_coarser_than_lower_resolution():
    """L0 (Global) must not be finer-grained than L1 (Micro)."""
    nodes, edges = _three_cluster_graph(clusters=4, per_cluster=6, inter=1.2)

    micro = len(set(DAEMON._leiden_partition(nodes, edges, DAEMON.LEIDEN_L1_RESOLUTION).values()))
    global_ = len(set(DAEMON._leiden_partition(nodes, edges, DAEMON.LEIDEN_L0_RESOLUTION).values()))

    assert global_ <= micro, f"L0={global_} L1={micro}"


def test_leiden_is_deterministic_for_a_fixed_seed():
    nodes, edges = _three_cluster_graph()

    first = DAEMON._leiden_partition(nodes, edges, 1.0)
    second = DAEMON._leiden_partition(nodes, edges, 1.0)

    assert first == second


def test_leiden_handles_empty_graph():
    assert DAEMON._leiden_partition([], [], 1.0) == {}


def test_leiden_ignores_self_loops_and_unknown_endpoints():
    nodes, edges = _three_cluster_graph()
    edges = [*edges, {"source": "C0_N0", "target": "C0_N0", "weight": 9.0},
             {"source": "C0_N0", "target": "ghost", "weight": 9.0}]

    partition = DAEMON._leiden_partition(nodes, edges, 1.0)

    assert set(partition) == set(nodes)


def test_leiden_survives_zero_and_missing_weights():
    nodes, edges = _three_cluster_graph()
    edges = [dict(edge) for edge in edges]
    edges[0]["weight"] = 0.0
    edges[1].pop("weight")

    partition = DAEMON._leiden_partition(nodes, edges, 1.0)

    assert set(partition) == set(nodes)


def test_daemon_no_longer_depends_on_python_louvain():
    source = (ROOT / "scripts" / "community_clustering_daemon.py").read_text(encoding="utf-8")
    assert "community_louvain" not in source
    assert "import leidenalg" in source
    assert "import igraph" in source


def test_community_page_template_carries_required_frontmatter():
    """The generated System_Community page must satisfy the schema validator."""
    source = (ROOT / "scripts" / "community_clustering_daemon.py").read_text(encoding="utf-8")
    template = source.split('content = f"""---', 1)[1].split('---', 1)[0]
    for field in ("id:", "title:", "type: system", "status:", "categories:", "updated:"):
        assert field in template, f"community template is missing {field!r}"


def test_community_page_template_passes_schema_validation(isolated_memory, monkeypatch):
    from vector_lake.schema_validator import validate_schema

    body = "\n".join([
        "---",
        "id: System_Community_L0_deadbeef",
        'title: "L0 Comm: A / B"',
        "type: system",
        "status: Active",
        "categories: [System]",
        "updated: 2026-01-01T00:00:00+00:00",
        "community_id: deadbeef",
        "level: L0",
        "aliases:",
        '- "L0 Comm: A / B"',
        "---",
        "# L0 Comm: A / B",
        "",
        "## 核心节点 (Hubs)",
        "- [[A]]",
    ])
    frontmatter, _ = __import__("vector_lake.wiki_utils", fromlist=["split_frontmatter"]).split_frontmatter(body)

    validate_schema(frontmatter, body, "System_Community_L0_deadbeef.md", None)
