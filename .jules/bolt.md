## 2026-06-26 - O(N^2) Inner Loop Redundant Dictionary Lookups
**Learning:** In Vector Lake's `indexer.py`, the edge calculation `_calculate_weighted_edges` iterates in $O(N^2)$. Inside this hot path, `calculate_relevance` was extracting the same attributes (type, decay_weight, alignment_score, etc) via dictionary `.get()` multiple times.
**Action:** Pre-compute node properties and sets in the outer $O(N)$ loop into dictionaries or parallel collections, and pass these pre-computed O(1) structures down to the $O(N^2)$ inner loop functions instead of accessing raw dictionaries repeatedly. Convert dynamically created sets within the inner loop to pre-computed `frozenset` objects.

## 2026-06-26 - Performance optimization in calculate_relevance

**Learning:** The `calculate_relevance` function in `vector_lake/indexer.py` is called O(N^2) times during graph edge calculation. Inside this function, there are loop constructs such as searching for links that match a target to find a triple predicate. These repeated list traversals for O(1) properties (e.g. predicates in triples) become a significant bottleneck for a large number of nodes.
**Action:** Optimize `calculate_relevance` by caching `triples_dict` mapping target -> predicate outside of the inner iterations, or preprocessing `triples` into a dictionary format when creating `node_a` and `node_b` during index parsing to avoid repeated iterations. Precomputing or caching dictionaries inside the nested loops is an important optimization pattern for this O(N^2) hot path.

## 2026-06-27 - Inline Optimization & Precomputation in O(N^2) loops
**Learning:** In `vector_lake/indexer.py`, isolating calculations out of the O(N^2) loop into O(1) structures (like precomputing link degrees, math.sqrt combinations, and static predicate weight evaluations) alongside eliminating the `calculate_relevance` function call overhead inside the tight loop provided massive latency improvements during heavy local graph generations.
**Action:** Always precompute any values and multipliers in an outer O(N) loop and inline simple arithmetic logic directly inside O(N^2) hot paths when micro-optimizations are required for scale.

## 2026-06-29 - O(N^2) Tuple Creation and Set Intersection
**Learning:** In Vector Lake's inner loop (`_calculate_weighted_edges`), repeatedly accessing dictionaries with tuple keys (like `(type_a, type_b)`) incurs significant overhead due to tuple creation and hashing inside the hot path. Similarly, computing set intersections (`sources_a & sources_b`) creates new Python objects even when the intersection is empty.
**Action:** Replace tuple-keyed dictionaries with nested dictionaries (`dict[type_a][type_b]`), allowing the outer key lookup to be hoisted out of the $O(N^2)$ loop. Guard set intersections with `.isdisjoint()`, which is heavily optimized in C and avoids allocating new set objects when there is no overlap.
