## 2024-04-21 - [Sanitize innerHTML to prevent XSS in topology.html]
**Vulnerability:** Cross-Site Scripting (XSS) via `innerHTML` interpolation of user-controlled markdown fields (e.g. node.name, node.group) in `templates/topology.html`.
**Learning:** Variables rendered into HTML attributes and content need explicit encoding using `escapeHTML` and JS attributes require `escapeJS` encoding. `escapeJS` must handle string escaping comprehensively, particularly escaping backslashes to avoid Javascript execution contexts breakouts. Also the values must be cast to Strings to prevent Type Confusion attacks.
**Prevention:** Always apply `escapeHTML()` to variables used in innerHTML, `escapeJS()` in event handlers, and strictly cast input `String()` in escape helpers to avoid Type Confusion.
