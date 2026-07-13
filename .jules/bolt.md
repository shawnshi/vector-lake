## 2023-10-25 - Delay deepcopy in O(N) search functions
**Learning:** In broad search operations over large in-memory caches (like `search_operational_memory`), performing `copy.deepcopy()` on every item that meets a minimal relevance threshold inside the hot loop dominates execution time.
**Action:** Always compute relevance scores and construct sort keys using references to the original objects. Slice the final `top_k` results first, then run `copy.deepcopy()` only on the elements that will actually be returned.
## 2023-10-25 - Avoid datetime.now() inside O(N) loops
**Learning:** In broad index parsing operations over thousands of markdown files (like `generate_index` and `update_index_items`), performing `datetime.now(timezone.utc)` inside the hot loop adds significant overhead due to system call repetition.
**Action:** Always compute `datetime.now()` once before the loop begins and pass the static pre-computed value down as an argument to inner parsing functions to save execution time.
