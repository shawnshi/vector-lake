## 2024-05-18 - [Fix DOM-based XSS in topology HTML template]
**Vulnerability:** DOM-based XSS when rendering topology data because values parsed from markdown (e.g. `node.id`, `node.group`, `node.name`, `node.claim_type`, and `labels[cid]`) were directly injected into the DOM using `.innerHTML` and unescaped template literals.
**Learning:** Raw data representing topology notes is parsed from wiki markdown files and visualized via D3/ForceGraph3D. This data bypasses server-side escaping as it is dynamically rendered client-side, necessitating explicit client-side sanitization before interpolation into the DOM.
**Prevention:** Always ensure dynamic data interpolated into string literals passed to `.innerHTML` is sanitized using an HTML escape function.
