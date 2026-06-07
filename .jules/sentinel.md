
## 2024-06-07 - [DOM XSS via Template Interpolation and InnerHTML]
**Vulnerability:** User-controlled graph node data was injected directly into `.innerHTML` and template literals without sanitization, leading to multiple DOM-based and Server-Side XSS vulnerabilities in `templates/topology.html` and `vector_lake/tool_graph.py`.
**Learning:** Developers frequently overlook the fact that JSON data containing HTML tags, when embedded inside an inline `<script>` block, can prematurely break out of the script tag. Additionally, directly mapping object properties into `.innerHTML` rather than `.textContent` is a common pitfall when building dynamic UI components without a framework like React.
**Prevention:** Always serialize JSON with unicode escaping for HTML characters (`<`, `>`, `&`) when embedding in script tags. Implement and strictly enforce the use of an `escapeHTML` helper function for any data entering `.innerHTML`, and ensure URL attributes (`href`) are wrapped in `encodeURI()`.
