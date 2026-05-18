## 2024-05-18 - [DOM XSS Prevention in Inline Handlers]
**Vulnerability:** Inline JavaScript handlers (like `onclick="selectNode('${targetId}')"`) were vulnerable to XSS and syntax breakage if `targetId` contained quotes, even after escaping HTML entities.
**Learning:** Directly interpolating user-controlled variables into inline event handlers is brittle. If a variable contains quotes, escaping it to `&quot;` inside an HTML attribute string that evaluates as JavaScript can cause parsing errors or lead to bypasses.
**Prevention:** Use HTML `data-*` attributes to store the safe data, and access it within the handler via `this.getAttribute('data-id')` to pass dynamic data securely without risking injection.

## 2024-05-18 - [Strict Type Checking in HTML Escapers]
**Vulnerability:** A simple `if (!str) return '';` check in an `escapeHTML` function dropped the number `0` because it evaluated as falsy in JavaScript, breaking UI stats elements.
**Learning:** Custom sanitizer functions must account for types like `0` or `false` which are valid and distinct from undefined or null.
**Prevention:** Always use strict checks (e.g., `if (str === undefined || str === null) return '';`) instead of truthiness checks when sanitizing mixed-type UI inputs.
