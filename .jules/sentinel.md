## 2025-05-28 - Fix Server-Side XSS in graph visualization payload
**Vulnerability:** XSS vulnerability in HTML visualization payload creation due to unescaped characters in JSON payload (`%%GRAPH_DATA%%`).
**Learning:** `json.dumps()` output directly embedded in `<script>` tags can be used to execute arbitrary JS when rendering malicious content containing `</script><script>alert(1)</script>`.
**Prevention:** Always escape `<` to `\u003c`, `>` to `\u003e`, and `&` to `\u0026` after serializing data to JSON for inclusion in HTML `<script>` blocks using string replacements.
