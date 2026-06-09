## 2024-10-24 - Server-Side XSS in JSON Serialization
**Vulnerability:** Server-Side XSS due to directly injecting `json.dumps` output into an HTML `<script>` block.
**Learning:** `json.dumps` with `ensure_ascii=False` doesn't escape HTML tags, making it vulnerable when placed inside `<script>` blocks if the data contains `</script>`.
**Prevention:** Chain replacements like `.replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')` on the resulting string to safely embed it in `<script>`.
