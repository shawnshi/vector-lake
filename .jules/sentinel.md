## 2024-05-06 - [High] DOM-based XSS in Visualizer Template
**Vulnerability:** The knowledge graph visualizer `templates/topology.html` constructed DOM elements using `innerHTML` with unsanitized data directly derived from nodes, groups, and semantic links.
**Learning:** `innerHTML` must never be directly manipulated using string interpolation of dynamically sourced application state (like node names, which might be attacker controlled).
**Prevention:** Use `textContent`/`innerText`, or proper `escapeHTML` logic for all dynamic data when working with HTML string templates in the frontend.
