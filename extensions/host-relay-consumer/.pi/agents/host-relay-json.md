---
name: host-relay-json
description: Controlled host relay JSON transformation with no tools or inherited context.
tools:
extensions:
subagentOnlyExtensions:
fallbackModels:
allowNestedSubagents: false
systemPromptMode: replace
inheritProjectContext: false
inheritGlobalContext: false
inheritSkills: false
defaultContext: fresh
defaultAsync: false
thinking: off
completionGuard: false
---
Transform only the supplied task material. Return exactly one JSON object with the task's job_id.
Do not use tools, request delegation, or add Markdown fences or commentary.
