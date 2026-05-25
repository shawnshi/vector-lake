## 2025-02-14 - Prevent Server-Side XSS in JSON-to-HTML template injection
**Vulnerability:** Directly injecting unescaped JSON strings (`json.dumps`) into HTML template `<script>` tags allowed for Server-Side XSS if a user-controlled field contained strings like `</script><script>alert(1)</script>`.
**Learning:** Python's standard `json.dumps` does not automatically escape HTML characters (`<`, `>`, `&`). When injecting the resulting JSON into an HTML context, these characters must be explicitly escaped.
**Prevention:** Always chain string replacements ` .replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')` onto the output of `json.dumps` before injecting it into HTML templates.
