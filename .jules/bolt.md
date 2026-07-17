## 2024-05-24 - Pre-populate nested dictionaries for O(1) lookups in O(N^2) loops
**Learning:** In Vector Lake's graph generation, an O(N^2) hot loop was executing dictionary `.get()` with fallbacks millions of times.
**Action:** When a fallback is known, hoist the fallback initialization above the O(N) loop and pre-populate the nested dictionaries (like `node_triples`) during the O(N) loop. This allows replacing `.get(key, default)` with direct `[key]` lookups inside the expensive O(N^2) inner loop.

## 2024-05-24 - Replace inner-loop dictionary lookups with frozenset membership tests
**Learning:** Searching for a key in a dictionary value (`key_a in node_links[key_b]`) inside an O(N^2) hot loop is very expensive if repeated.
**Action:** Pre-compute reverse relationship maps (`reverse_links`) as `frozenset` objects. Cache the lookup `reverse_links.get(key_a, frozenset())` at the top of the outer loop so the inner loop check becomes a simple O(1) `key_b in reverse_links_a` membership test.

## 2024-07-14 - Unused O(N) Array Allocations in Search Hot Path
**Learning:** Codebase sometimes retains expensive list comprehensions iterating over the entire global index inside search functions (e.g. `nodes = [...]` in `tool_search.py`), which are never consumed.
**Action:** Always check if variables returned from O(N) operations in hot paths are actually used before optimizing them.

## 2024-05-24 - Avoid dictionary copies and nested comprehensions in O(N) paths
**Learning:** In `compute_debt_metrics`, calling `annotate_claim_validity` on every claim created thousands of unnecessary dictionary copies just to read one field. Additionally, building `source_ids_with_claims` via a nested list comprehension iterated over all claims a second time.
**Action:** When only specific fields are needed, call the underlying function (`infer_claim_validity`) instead of the wrapper that copies dictionaries. Aggregate secondary data (`source_ids_with_claims.update()`) within the same single pass loop to avoid redundant O(N) iterations.

## 2024-05-24 - Use defaultdict for fast incrementing instead of .get()
**Learning:** Initializing frequencies with `dict.get(key, 0) + 1` incurs overhead for missing keys.
**Action:** Use `collections.defaultdict(int)` to allow `dict[key] += 1` for O(1) increments.
