## YYYY-MM-DD - [Prevent DOM XSS in 3D Graph Render]
**Vulnerability:** DOM-based Cross-Site Scripting (XSS) in templates/topology.html where node attributes like `node.name`, `node.group`, and `node.id` were directly concatenated into `.innerHTML`.
**Learning:** Client-side templates parsing JSON graph data must ensure strict encoding or DOM-based construction. Utilizing `document.createElement()` along with closure event handlers completely eliminates risks from interpolating dynamic inputs into inline attributes like `onclick`.
**Prevention:** Always escape user-controlled variables using robust HTML entity encoding when injecting into `.innerHTML`. Prefer `document.createElement` + `textContent` for dynamic lists or links instead of string concatenation.
