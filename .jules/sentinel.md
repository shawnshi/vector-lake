## 2024-05-14 - Fix DOM XSS in Topology Template
**Vulnerability:** Found multiple instances of Cross-Site Scripting (XSS) vulnerabilities where dynamically injected node parameters (such as `node.name`, `node.id`, etc.) were directly assigned to `innerHTML` in the `templates/topology.html` script.
**Learning:** This occurred because the code prioritized quick string concatenation and templating over safe DOM manipulation for rendering complex structures like the info panel and links.
**Prevention:** Avoid interpolating variables into `innerHTML` strings. Use `document.createElement`, `textContent`, and safe event bindings like `addEventListener` or `a.onclick = function() {...}`. Pass dynamic data safely via `data-*` attributes.
