## 2026-05-23 - XSS via JSON Injection in HTML Script Tags
**Vulnerability:** Cross-Site Scripting (XSS) via unescaped JSON strings injected into HTML script blocks
**Learning:** When injecting serialized JSON directly into a script tag using string replacement, characters like `<`, `>`, and `&` can prematurely close the script block if a user embeds `</script>` in their data.
**Prevention:** Always escape `<` as `\u003c`, `>` as `\u003e`, and `&` as `\u0026` in JSON strings before inserting them into HTML templates.
