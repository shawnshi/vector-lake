
## 2024-06-22 - Prevent Server-Side and DOM XSS in Visualization Templates
**Vulnerability:** Critical Cross-Site Scripting (XSS) in `templates/topology.html` due to unsafe JSON injection (`%%GRAPH_DATA%%`) and widespread DOM injection via `.innerHTML` (e.g., node titles, metadata).
**Learning:** Python's `json.dumps` does not automatically escape HTML control characters like `<` and `>`, leaving `<script>` payloads executable when interpolated directly into an HTML template. Additionally, UI code relied extensively on `.innerHTML` for rendering node metadata instead of safer text insertion methods.
**Prevention:** In Python, chain `.replace('<', r'\u003c')` after `json.dumps(..., ensure_ascii=False)` before injecting into script blocks. In JS, define a global `escapeHTML` function and apply it to all untrusted string variables before `.innerHTML` assignment. Avoid `.innerHTML` where `.textContent` suffices.
