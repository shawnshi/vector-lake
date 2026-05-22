## 2026-05-22 - Server-Side XSS in JSON-to-HTML Injection
**Vulnerability:** XSS via unsanitized JSON string injection into HTML `<script>` block during graph generation.
**Learning:** `json.dumps` does not automatically escape `<` or `>` characters. When injecting directly into `%%GRAPH_DATA%%` inside a `<script>` tag, an attacker who controls data in `graph_data` can input `</script><script>alert(1)</script>` which prematurely closes the script tag and executes arbitrary XSS.
**Prevention:** When injecting JSON directly into HTML script tags, ALWAYS apply `.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')` to safely encode angle brackets and ampersands, maintaining valid JSON structure while nullifying HTML element parsing.
