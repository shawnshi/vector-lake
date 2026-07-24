
## 2024-05-27 - Fix DOM-based XSS in topology graph links
**Vulnerability:** Found a DOM-based XSS vulnerability in `templates/topology.html` where unvalidated URL schemes (like `javascript:`) in graph nodes could execute arbitrary JavaScript when a node was clicked and rendered in the DOM.
**Learning:** Using `encodeURI()` is insufficient for security in dynamic href injection because it does not filter pseudo-protocols.
**Prevention:** Always validate URL schemes against a strict allowlist (e.g. http, https, file, /, #) using a dedicated sanitizeURL wrapper before injecting dynamic URL data into anchor elements.
