
## 2024-06-22 - Prevent Server-Side and DOM XSS in Visualization Templates
**Vulnerability:** Critical Cross-Site Scripting (XSS) in `templates/topology.html` due to unsafe JSON injection (`%%GRAPH_DATA%%`) and widespread DOM injection via `.innerHTML` (e.g., node titles, metadata).
**Learning:** Python's `json.dumps` does not automatically escape HTML control characters like `<` and `>`, leaving `<script>` payloads executable when interpolated directly into an HTML template. Additionally, UI code relied extensively on `.innerHTML` for rendering node metadata instead of safer text insertion methods.
**Prevention:** In Python, chain `.replace('<', r'\u003c')` after `json.dumps(..., ensure_ascii=False)` before injecting into script blocks. In JS, define a global `escapeHTML` function and apply it to all untrusted string variables before `.innerHTML` assignment. Avoid `.innerHTML` where `.textContent` suffices.
## 2024-06-26 - Prevent SQL Injection in Dynamic Table Name Resolution
**Vulnerability:** CRITICAL SQL injection risk in `vector_lake/governance_store.py` where `table_name` was directly interpolated into SQL queries (e.g., `f"SELECT * FROM {table_name}"`) without any validation or parameterization.
**Learning:** In SQLite, table names cannot be parameterized using `?`. Therefore, any dynamic construction of queries involving table names must use a strict allowlist. Without it, any exposed helper function could lead to arbitrary SQL execution.
**Prevention:** Implement an `ALLOWED_TABLES` set containing all valid table names and a validation function `_validate_table_name` that raises a `ValueError` if an unexpected table name is provided. Call this validation at the entry point of all generic database operations.
## 2024-06-29 - Prevent SQL Injection in Dynamic WHERE Clauses
**Vulnerability:** CRITICAL SQL injection risk in `vector_lake/governance_store.py` (`query_entities` and `query_memory_objects`) where dictionary keys from the `filters` argument were directly interpolated into the SQL `WHERE` clause without validation.
**Learning:** In SQLite (and SQL in general), column names in a `WHERE` clause cannot be parameterized using placeholders (like `?`). Allowing users to pass arbitrary string keys in a filter dictionary that maps directly to SQL clauses creates a severe injection point.
**Prevention:** Always validate dynamically generated column names or filter keys against a strict regex (e.g., `^[a-zA-Z0-9_]+(!=)?$`) or a predefined allowlist of valid columns before interpolating them into a SQL query.
