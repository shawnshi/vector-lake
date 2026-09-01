---
name: daemon-watchdog
metadata:
  version: 11.20.0
  tier: action-allowed
description: Start or stop the Vector Lake watchdog from the source tree actually serving the connected MCP runtime.
---

# Watchdog lifecycle

Use this skill only for an explicit request to start or stop the persistent watchdog.

1. Call `mcp_runtime_status` and parse its JSON. Require `stale=false` and an absolute `source_root`; otherwise stop and report the authority failure.
2. Resolve the plugin root as the parent of `source_root` when it names the `vector_lake` package. Require that root to contain both `watchdog_sync.py` and `.mcp.json`.
3. Never substitute a fixed user-profile path, an old checkout, or a guessed plugin directory for `source_root`.
4. Launch `watchdog_sync.py` as a background process with the resolved plugin root as its working directory. For stop requests, invoke the same authoritative script with `--stop`.
5. Let the entrypoint bootstrap the paired MEMORY/meta paths from its own `.mcp.json`; do not combine a process override from one runtime with configuration from another.
6. Verify the new process remains alive and that its status belongs to the same effective meta root before reporting success. A PID alone is not health.
