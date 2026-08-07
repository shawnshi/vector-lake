"""Short-lived Louvain worker so heavy scientific imports never stick in daemons."""

from __future__ import annotations

import json
import math
import sys


def _validated_payload(payload: object) -> tuple[list[str], list[tuple[str, str, float]]]:
    if not isinstance(payload, dict):
        raise ValueError("topology payload must be an object")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("topology nodes and edges must be arrays")
    nodes = [str(node) for node in raw_nodes]
    if len(nodes) != len(set(nodes)):
        raise ValueError("topology nodes must be unique")
    node_set = set(nodes)
    edges: list[tuple[str, str, float]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, list) or len(raw_edge) != 3:
            raise ValueError("topology edge must contain source, target, and weight")
        source = str(raw_edge[0])
        target = str(raw_edge[1])
        weight = float(raw_edge[2])
        if source not in node_set or target not in node_set:
            raise ValueError("topology edge references an unknown node")
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("topology edge weight must be finite and non-negative")
        edges.append((source, target, weight))
    return nodes, edges


def main() -> int:
    try:
        nodes, edges = _validated_payload(json.load(sys.stdin))
        import networkx as nx
        from community import community_louvain

        graph = nx.Graph()
        graph.add_nodes_from(nodes)
        graph.add_weighted_edges_from(edges)
        partition = community_louvain.best_partition(
            graph,
            weight="weight",
            random_state=0,
        )
        json.dump(
            {"partition": partition},
            sys.stdout,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
