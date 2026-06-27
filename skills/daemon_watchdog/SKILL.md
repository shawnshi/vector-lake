---
name: daemon_watchdog
description: 'Launch the real-time ingest watcher as the long-running background compiler for raw sources.'
---
Please start the long-running Vector Lake watchdog by using the `run_command` tool.
Execute `$env:PYTHONIOENCODING="utf-8"; python C:\Users\shich\.gemini\config\plugins\vector-lake\watchdog_sync.py` with `WaitMsBeforeAsync` set to `2000` to ensure it launches as a background task. 
This is the persistent daemon path for ingest monitoring; do not substitute a one-off `sync` run for it. After starting it, tell the user that the watchdog daemon is running in the background.
