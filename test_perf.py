import time
from vector_lake.indexer import _calculate_weighted_edges

def make_node(i):
    return {
        "title": f"Node {i}",
        "links": [f"n_{j}" for j in range(max(0, i-5), i)],
        "sources": [f"src_{i%3}"],
        "type": "concept",
        "decay_weight": 0.9,
        "alignment_score": 80.0,
        "triples": [{"target": f"n_{j}", "predicate": "mentions"} for j in range(max(0, i-2), i)]
    }

nodes = {f"n_{i}": make_node(i) for i in range(1000)}
index_data = {"nodes": nodes}

start = time.time()
_calculate_weighted_edges(index_data)
print(f"Nodes: {len(nodes)}, Time: {time.time() - start:.4f}s")
