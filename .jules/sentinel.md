## 2026-05-02 - Prevent DOM XSS in Topology Template
**Vulnerability:** DOM-based XSS through direct `innerHTML` interpolation of unsanitized node attributes (like name, id, group).
**Learning:** When using vanilla JavaScript to build UI from dynamic JSON data derived from markdown, inserting values directly into `innerHTML` or inline event handlers is unsafe. The `onclick="selectNode('${targetId}')"` pattern breaks if `targetId` contains quotes.
**Prevention:** Always define an escaping function for variables inserted into `innerHTML` and prefer HTML `data-*` attributes for passing dynamic values to event listeners instead of interpolating strings directly into inline handlers.
