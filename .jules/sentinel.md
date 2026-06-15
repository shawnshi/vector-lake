## 2024-06-15 - Fix DOM XSS in Topology Viewer
**Vulnerability:** DOM-based XSS where unescaped graph metadata (node name, type, state, etc.) was interpolated directly into `.innerHTML` during UI updates.
**Learning:** The static HTML frontend uses vanilla JS to dynamically construct DOM elements. Variables bound to user-controllable data must be properly encoded before assignment to `.innerHTML`, even inside strings/template literals.
**Prevention:** Use an `escapeHTML` helper function to escape all dynamic fields before inserting them via `.innerHTML`, or use `.textContent`/`.innerText` which handles escaping naturally.
