## 2024-05-18 - Prevent DOM-based XSS in Topology Graph Template
**Vulnerability:** DOM-based Cross-Site Scripting (XSS) in `templates/topology.html` due to unsafe `.innerHTML` assignments and string interpolation in inline `onclick` handlers.
**Learning:** Dynamic data loaded from backend graphs (e.g., node names, groups) can contain HTML tags or quotes, allowing script injection when directly injected into `.innerHTML`.
**Prevention:** Use `document.createElement()` and set `.textContent` for dynamic values instead of building HTML strings. Avoid interpolating strings in inline event handlers; attach custom data via HTML `data-*` attributes and retrieve them inside the handler using `.getAttribute()`.
