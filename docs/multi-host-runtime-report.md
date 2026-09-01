# Multi-host runtime capacity and supervision report

- Measured: 2026-08-31
- Repository HEAD: `5267575ca0d4db25fdcb436bea8b2ce6fc4d0ba9`
- State: dirty working tree; results apply to the file hashes below, not bare HEAD
- Platform: Windows 11 `10.0.28020`, Python `3.13.12`

## Scope

The benchmark launched three independent **full-surface** stdio MCP processes and one watchdog against a fresh isolated temporary MEMORY root. Each MCP completed `initialize` and `tools/list`; the soak then called `mcp_runtime_status` sequentially across all three processes. RSS includes each measured root process and its recursive children.

This measures Vector Lake child-process capacity. It does not include the resident RSS of the Codex, Pi, or Gemini host applications themselves. No user MEMORY, user configuration, external model, or network service was used. Gemini CLI was not installed, so this is not a Gemini-host end-to-end claim.

Command:

```powershell
python scripts/benchmark_multi_host_runtime.py `
  --duration-seconds 300 `
  --max-total-rss-mib 1024 `
  --max-runtime-status-p95-ms 250 `
  --max-mcp-startup-ms 5000
```

## Result

| Check | Observed | Gate | Result |
|---|---:|---:|---|
| Full MCP tool count | `64 / 64 / 64` | exactly `64` each | pass |
| MCP startup | `1999.667 / 1624.490 / 1701.165 ms` | max `5000 ms` | pass |
| Watchdog startup | `461.685 ms` | informational | pass |
| `mcp_runtime_status` calls | `2010` | no missing responses | pass |
| Runtime-status P50 | `41.455 ms` | informational | pass |
| Runtime-status P95 | `55.928 ms` | `≤250 ms` | pass |
| Runtime-status max | `131.079 ms` | informational | pass |
| Runtime stale reports | `0` | `0` | pass |
| Three MCP peak RSS | about `119.5 / 119.3 / 119.2 MiB` | informational | pass |
| Watchdog peak RSS | `58.840 MiB` | informational | pass |
| Total peak RSS | `416.742 MiB` | `≤1024 MiB` | pass |
| Total steady RSS | `416.742 MiB` | informational | pass |

The measured process set used 40.7% of the 1 GiB gate, leaving 607.258 MiB of headroom. Peak RSS did not exceed the final sample during the five-minute window. This bounded run is evidence against immediate growth, not a proof that longer or workload-heavy sessions cannot grow.

## Supervision contract

The watchdog now applies a bounded worker restart budget (`VECTOR_LAKE_WATCHDOG_WORKER_RESTART_LIMIT`, default `2`):

- transient worker exits create a fresh thread and remain visible through component status/logs;
- `outbox`, `ingest`, and `auto_ingest` remain fail-closed after the budget is exhausted;
- `scheduler` is non-critical by default: exhaustion isolates that component while outbox, ingest, filesystem observation, and the watchdog heartbeat continue;
- operators may make the scheduler critical through `VECTOR_LAKE_WATCHDOG_REQUIRED_COMPONENTS`;
- runtime health and Deep Doctor share one classifier: an isolated optional scheduler is a warning, while required-component failure remains blocking.

Deterministic tests cover transient restart, scheduler isolation, required-worker halt, and the health classification boundary.

## Decision

Independent stdio MCP processes satisfy the current capacity and latency gates. Do **not** add shared HTTP transport, multiplexing, or a process-sharing layer in this release: those mechanisms would add authentication, lifecycle, routing, and failure-coupling costs without a measured capacity need.

## Release validation status

The exact working tree has passed the deterministic local release gates and independent structural review; the scoped P0–P2 implementation is **release-green**:

- the complete suite passed `1983` tests and `300` subtests and skipped `1`; no test was deselected;
- complete projection materialization now traverses trie frontiers in order while prefetching at most `256` immutable objects per batch through four bounded threads. Serial pagination and diff traversal are unchanged;
- every object still receives its existing `lstat/open/fstat/read/fstat/SHA-256`, strict JSON, canonical-byte, and node-shape checks. Each shard directory is validated before its first scheduled object and identity-checked again after the complete traversal; worker or boundary failures remain fail-closed;
- the 126,885-node isolated cold read passed at `1.374624 s`, leaving `1.125376 s` (`45.0%`) below the unchanged `2.5 s` SLO. The warm cache hit was `0.003662 s`;
- the fully durable single-item benchmark also passed: 126,885-node p95 latency was `0.030871 s` and p95 new bytes were `11,722.15`, so read concurrency did not alter the canonical format or write path;
- the earlier bounded hot-path trial that failed the full suite remains preserved only as an audit artifact and was rolled back byte-for-byte before this structural implementation;
- the Windows scheduler-isolation test uses the production tolerant status reader; the focused lifecycle/runtime-health/Doctor set passed `115` tests;
- earlier independent review closed CWD-relative runtime roots and Deep Doctor's optional-scheduler classification. A separate reviewer confirmed the watchdog race fix, failed performance-trial rollback, and report evidence. Final structural review reported no P0/P1/P2 findings and returned `OK with notes`;
- a post-structural-change 15-second isolated smoke exposed `64 / 64 / 64` tools, reported `0` stale revisions, passed at `415.312 MiB` total peak RSS and `36.368 ms` status P95, and left no Python MCP/watchdog child process behind;
- focused LSP validation is clean for the new projection-store code and tests. Existing findings outside this change are not claimed as resolved;
- `python -m pip check`, `compileall`, Ruff `E4/E7/E9/F`, changed-file Ruff, `git diff --check`, and a tracked-plus-untracked credential-pattern scan passed. `gitleaks` was unavailable, and the repository has no clean 88-character `E501` baseline;
- Gemini remains manifest/raw-stdio only because the Gemini CLI is not installed. Pi and Codex user configuration was not changed or reloaded.

Cold-read evidence across the original and authorized rounds is:

| Context | Cold committed read | Outcome |
|---|---:|---|
| Original full suite | `2.760753 s` | fail |
| Original final isolated attempt | `2.505014 s` | fail |
| Authorized fresh isolated baseline | `2.785331 s` | fail |
| First bounded patch, isolated | `2.477675 s` | pass |
| First bounded patch, full suite | `2.956014 s` | fail; rolled back |
| Bounded structural prefetch, isolated | `1.374624 s` | pass |
| Bounded structural prefetch, full suite | `<2.5 s` assertion passed | pass |

The profiled fixture loaded `4,370` immutable objects. Under cProfile overhead, `_secure_read_object` accounted for `2.074 s` of a `3.909 s` wall time, including `0.715 s` in reads and `0.448 s` in `lstat`; node validation accounted for `0.709 s`. The final design overlaps only independent file reads, retains bounded task submission and serial semantic validation, and does not change persistent objects, sidecars, canonical generations, mutation behavior, or host configuration.

Accordingly, the scoped P0–P2 implementation is `DONE` and supports the independent-stdio architecture and supervision policy. This is a source-tree release verdict, not evidence that user host configuration was reloaded or that Gemini completed an end-to-end host launch. The hash table below identifies the five-minute measurement snapshot; later Doctor and projection-read changes do not rewrite that historical measurement.

## Bound file hashes

| File | SHA-256 |
|---|---|
| `scripts/benchmark_multi_host_runtime.py` | `c151f943f03ba831203c42fa5b28b271b789f887cf518a93345ae4c5b72bec46` |
| `scripts/vector_lake_mcp.py` | `60cb7fa1d6b3e9da32e589d927c58e343b5467f4e01195493918a5f28350ed89` |
| `vector_lake/mcp_server.py` | `b1db0cb68b2127602e7258dfcd026f38cf2bc69857cba3c6c62e615583b203d8` |
| `vector_lake/watchdog_app.py` | `97471ed8d6caa4df4394bc883fa2d405e88f2b24a22a4ebe0b9fbdcf26e07ab4` |
| `vector_lake/runtime_health.py` | `710e766102be211a2e0688298443f369de5d98bbd48217c0cf7b359af1829053` |
| `runtime_profiles.json` | `7470c03da066528bead95895bb6e8a14ed41b6434a8a1cc560616cd21c7d7033` |

Raw benchmark JSON was written to `C:/Users/shich/AppData/Local/Temp/vector-lake-p2-capacity-result-300s.json` for local audit. It is not a release contract and may be removed after review.
