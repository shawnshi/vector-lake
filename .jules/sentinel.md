## 2025-03-08 - Prevent DOM XSS in string template interpolation
**Vulnerability:** DOM-based Cross-Site Scripting (XSS) due to unsafe interpolation of variables directly into innerHTML.
**Learning:** In projects without templating engines that auto-escape output, direct string concatenation or template literal insertion into `.innerHTML` (e.g., `` element.innerHTML = `<p>${variable}</p>`; ``) exposes the application to DOM XSS if the variable contains user-controlled or dynamically generated data.
**Prevention:** Implement and enforce the use of a safe HTML escaping function (`escapeHTML`) before assigning data to `.innerHTML` or `.outerHTML`, and securely encode URLs with `encodeURI()` before placing them in `href` attributes.
