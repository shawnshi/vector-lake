## 2025-02-14 - Prevent Server-Side XSS in JSON-in-HTML serialization
**Vulnerability:** Server-Side XSS via unescaped JSON injection into a `<script>` block in `templates/topology.html`.
**Learning:** Using `json.dumps` directly inside a `<script>` tag is unsafe because characters like `<` and `>` can be parsed by the browser as HTML tags before the JavaScript engine executes the JSON payload.
**Prevention:** Always escape `<` to `\u003c`, `>` to `\u003e`, and `&` to `\u0026` using raw string replacements or double escaping after JSON serialization when injecting into HTML templates.
