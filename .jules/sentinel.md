## 2024-05-24 - DOM XSS via Unescaped Template Variables in HTML

**Vulnerability:** Found multiple instances of DOM-based Cross-Site Scripting (XSS) in `templates/topology.html` where dynamically populated fields originating from parsed markdown content (like `node.name`, `node.group`, `node.id`, etc.) were interpolated into HTML templates and rendered directly via `.innerHTML`.

**Learning:** When ingesting external sources into visual components, any string interpolation intended for the DOM risks XSS if users can influence those source files. Using direct string replacement in `innerHTML` makes this worse. Additionally, ensuring the custom `escapeHTML` function performs strict checks (`if (str === undefined || str === null) return '';`) avoids dropping falsy but valid entries like `0` or `false`.

**Prevention:** Ensure all user-controlled or dynamically injected variables are sanitized with a robust escaping function before passing them to `.innerHTML`. Where possible, rely on `.textContent` or `.innerText` which are natively safe against DOM XSS.
