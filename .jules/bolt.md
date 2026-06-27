## 2026-06-26 - O(N^2) Inner Loop Redundant Dictionary Lookups
**Learning:** In Vector Lake's `indexer.py`, the edge calculation `_calculate_weighted_edges` iterates in $O(N^2)$. Inside this hot path, `calculate_relevance` was extracting the same attributes (type, decay_weight, alignment_score, etc) via dictionary `.get()` multiple times.
**Action:** Pre-compute node properties and sets in the outer $O(N)$ loop into dictionaries or parallel collections, and pass these pre-computed O(1) structures down to the $O(N^2)$ inner loop functions instead of accessing raw dictionaries repeatedly. Convert dynamically created sets within the inner loop to pre-computed `frozenset` objects.

## 2026-06-26 - Performance optimization in calculate_relevance

**Learning:** The `calculate_relevance` function in `vector_lake/indexer.py` is called O(N^2) times during graph edge calculation. Inside this function, there are loop constructs such as searching for links that match a target to find a triple predicate. These repeated list traversals for O(1) properties (e.g. predicates in triples) become a significant bottleneck for a large number of nodes.
**Action:** Optimize `calculate_relevance` by caching `triples_dict` mapping target -> predicate outside of the inner iterations, or preprocessing `triples` into a dictionary format when creating `node_a` and `node_b` during index parsing to avoid repeated iterations. Precomputing or caching dictionaries inside the nested loops is an important optimization pattern for this O(N^2) hot path.
