## 2024-05-24 - Pre-populate nested dictionaries for O(1) lookups in O(N^2) loops
**Learning:** In Vector Lake's graph generation, an O(N^2) hot loop was executing dictionary `.get()` with fallbacks millions of times.
**Action:** When a fallback is known, hoist the fallback initialization above the O(N) loop and pre-populate the nested dictionaries (like `node_triples`) during the O(N) loop. This allows replacing `.get(key, default)` with direct `[key]` lookups inside the expensive O(N^2) inner loop.

## 2024-05-24 - Replace inner-loop dictionary lookups with frozenset membership tests
**Learning:** Searching for a key in a dictionary value (`key_a in node_links[key_b]`) inside an O(N^2) hot loop is very expensive if repeated.
**Action:** Pre-compute reverse relationship maps (`reverse_links`) as `frozenset` objects. Cache the lookup `reverse_links.get(key_a, frozenset())` at the top of the outer loop so the inner loop check becomes a simple O(1) `key_b in reverse_links_a` membership test.

## 2024-07-14 - Unused O(N) Array Allocations in Search Hot Path
**Learning:** Codebase sometimes retains expensive list comprehensions iterating over the entire global index inside search functions (e.g. `nodes = [...]` in `tool_search.py`), which are never consumed.
**Action:** Always check if variables returned from O(N) operations in hot paths are actually used before optimizing them.
## 2024-05-24 - Use defaultdict over dict.get() for high-frequency counting loops
**Learning:** In Vector Lake, `compute_debt_metrics` iterates over thousands of claims and increments dictionary counters. `dict.get(key, 0) + 1` incurs function call overhead, missing key handling, addition, and re-assignment on every single loop iteration.
**Action:** When counting frequencies inside hot paths, prefer `collections.defaultdict(int)`. This eliminates the missing key check and reduces the operation to an in-place `+= 1` allocation, saving CPU cycles during bulk operations. Cast back to `dict()` before returning if downstream consumers require standard dictionary semantics.
