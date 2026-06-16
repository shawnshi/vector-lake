## 2026-06-16 - Prevent XSS in Topology Graph Template and Graph Data

**Vulnerability:** DOM-based Cross-Site Scripting (XSS) via `innerHTML` injection in the topology graph template, and Server-Side XSS via direct injection of JSON data into the HTML payload.

**Learning:** The topology template dynamically constructs and renders anchor tags based on node data without proper sanitization. Additionally, JSON payloads containing user data were injected directly into `<script>` tags, making it possible to execute arbitrary JavaScript when `GRAPH_DATA` contained unescaped `<script>` or other HTML tags.

**Prevention:**
1. Always implement and utilize an `escapeHTML()` function when working with user data that may be reflected into the DOM, avoiding truthy/falsy checks that drop values like `0` or `false`.
2. When creating DOM anchor tags, encode URLs using `encodeURI()`.
3. To prevent injection of untrusted user data into `<script>` context, ensure that Python dictionaries serialized using `json.dumps()` are correctly escaped using `replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')` before being interpolated into HTML templates.
