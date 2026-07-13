## 2023-10-25 - Delay deepcopy in O(N) search functions
**Learning:** In broad search operations over large in-memory caches (like `search_operational_memory`), performing `copy.deepcopy()` on every item that meets a minimal relevance threshold inside the hot loop dominates execution time.
**Action:** Always compute relevance scores and construct sort keys using references to the original objects. Slice the final `top_k` results first, then run `copy.deepcopy()` only on the elements that will actually be returned.
