## 2026-05-07 - DOM XSS in Event Handlers
**Vulnerability:** XSS via innerHTML and onclick in topology UI.
**Learning:** Dynamic data interpolation directly into inline event handlers (like onclick) or via innerHTML introduces XSS risks, even with mostly static backend data, if the node names or groups contain unescaped content.
**Prevention:** Use data-* attributes and DOM APIs (createElement, textContent) for safe dynamic rendering.
