## YYYY-MM-DD - [Title]
**Vulnerability:** [What you found]
**Learning:** [Why it existed]
**Prevention:** [How to avoid next time]
## 2024-05-18 - Prevent DOM XSS in topology visualizer template
**Vulnerability:** The HTML template `templates/topology.html` dynamically assigned variables to DOM `innerHTML` properties using string interpolation without escaping, which could allow malicious node data to execute as an XSS vector. Additionally, variables were interpolated into inline `onclick` handler arguments which could break JavaScript syntax or evaluate malicious input.
**Learning:** This template generates UI directly from JSON outputs. When parsing dynamic structural data (like entity names, metadata values, link identifiers) into HTML format, even locally provided graph data must be fully sanitized to prevent DOM-based XSS when loaded via the browser.
**Prevention:** Added a global `escapeHTML` helper function within the template to encode dangerous HTML characters. Wrapped all dynamic variables parsed into `innerHTML` strings with this function, and switched inline event handlers to secure generic event delegation with DOM `data-*` attributes.
