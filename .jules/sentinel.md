
## 2024-06-11 - Prevent Server-Side XSS in JSON-to-Script Serialization
**Vulnerability:** When Python dictionaries are serialized to JSON using `json.dumps()` and injected directly into HTML `<script>` tags, malicious payloads containing `<script>` or `</script>` are evaluated by the browser because the HTML parser takes precedence over JavaScript parsing.
**Learning:** Python's standard `json.dumps()` does not escape HTML characters like `<`, `>`, or `&` by default. Even when `ensure_ascii=False` is set, HTML entity sequences are passed unescaped, allowing Cross-Site Scripting (XSS).
**Prevention:** To safely serialize JSON for injection into a `<script>` block in Python, manually escape HTML characters in the resulting string by chaining string replacements: `json.dumps(data, ensure_ascii=False).replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')`.
