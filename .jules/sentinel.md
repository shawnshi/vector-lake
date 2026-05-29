## 2025-05-29 - [Fix Server-Side XSS in JSON HTML embedding]
**Vulnerability:** XSS vulnerability where JSON encoded payload in `vector_lake/tool_graph.py` wasn't properly escaped before embedding into HTML.
**Learning:** `json.dumps()` doesn't escape HTML tags (`<`, `>`, `&`). If user content has `</script>`, it can escape script block to execute arbitrary JS in an HTML context.
**Prevention:** Always use safe string replacement for `<`, `>`, and `&` (`\u003c`, `\u003e`, `\u0026`) when injecting JSON blobs directly inside inline HTML script tags.
