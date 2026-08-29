---
name: graph
metadata:
  version: 11.20.0
  tier: action-allowed
description: Render the current Vector Lake topology as an interactive HTML graph in an approved agent sandbox.
---

# Graph visualization

Call `visualize_vector_lake` from `vector-lake-mcp` with an absolute `output_dir` inside the host-approved `.codex` or `.gemini` scratch area.

- Do not use fabricated nodes or a stale exported dataset.
- Do not redirect output to a global, repository, or user-selected directory unless that location is explicitly authorized and accepted by the tool.
- Verify that the returned HTML file exists and is inside the requested sandbox.
- Return a clickable absolute path and disclose any truncation, omitted topology, or rendering warning.
