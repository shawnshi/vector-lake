## 2024-05-27 - DOM-based XSS via Missing Protocol Validation
**Vulnerability:** A DOM-based XSS existed in `templates/topology.html` where node title anchor tags were dynamically generated via `.innerHTML` using `encodeURI(nodeHref)`.
**Learning:** `encodeURI()` alone is insufficient to prevent XSS in `href` attributes because it explicitly permits URI scheme declarations like `javascript:`. Attackers controlling the node ID or href could inject `javascript:alert(1)` to achieve arbitrary code execution.
**Prevention:** When dynamically assigning untrusted data to `href` attributes (especially via `.innerHTML`), always validate the protocol against a strict allowlist (e.g., `^(https?|file|\/|\#)`) before rendering the tag.
