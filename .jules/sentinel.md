## 2024-05-24 - Server-Side XSS via JSON Injection
**Vulnerability:** XSS via unescaped HTML characters when directly injecting JSON into a <script> block.
**Learning:** json.dumps() does not escape HTML control characters like < and >, allowing payloads to break out of script tags.
**Prevention:** Apply chained string replacements (e.g., .replace('<', r'\u003c')) to safely escape these characters before HTML injection.
