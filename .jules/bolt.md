## 2024-07-14 - Unused O(N) Array Allocations in Search Hot Path
**Learning:** Codebase sometimes retains expensive list comprehensions iterating over the entire global index inside search functions (e.g. `nodes = [...]` in `tool_search.py`), which are never consumed.
**Action:** Always check if variables returned from O(N) operations in hot paths are actually used before optimizing them.
