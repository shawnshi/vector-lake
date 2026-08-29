# Vector Lake 11.20.0

## P2/P3 architecture remediation

- Upgraded SQLite to schema v9 with an authoritative `projection_runtime_v9`
  publish state. Controlled v4-v8 inputs migrate to v9, while rollback accepts
  only the matching completed v8-to-v9 receipt and restores a verified v8
  database/projection boundary.
- Replaced full projection rewrites with content-addressed immutable projection
  v2 objects and a sidecar commit pointer. Incremental page changes update only
  affected HAMT paths and a bounded 512-node topology frontier; unchanged
  generations are byte- and mtime-idempotent.
- Allowed the explicit projection-rebuild migration command to read a retained
  v1 pair only while schema v9 is pointer-free and `rebuild_required`. It
  verifies the pair, sidecar, canonical generation and stable file identities;
  ordinary readers remain fail-closed, and apply backs up before publication.
  Legacy maintenance backup/restore accepts bounded files through 128 MiB with
  actual-byte capacity preflight and exact hash validation; v2 object limits
  remain unchanged.
- Added maintenance-backup v4, transitive object-closure validation, generic
  receipt-bound snapshot restore, and preview/fingerprint/apply projection-object
  collection that protects live, pending, previous, backup and recovery roots.
- Added a configurable durability profile. The default full profile applies
  file and directory persistence barriers to acknowledged Wiki, projection,
  backup and receipt publications; failure injection preserves an old-or-new
  complete generation.
- Made automatic-ingest budgets exactly observable and added bounded,
  resumable attempt-receipt retention. The 100/hour, 2,000/24-hour and
  32,768-token limits remain ceilings, and raw-text processing stays disabled
  and unapproved by default.
- Added a persistent raw-scrub ledger and daily due/retry scheduling so missed
  filesystem events are rehashed within the configured period across restarts.
- Batched embedding metadata/vector writes into one generation-CAS transaction
  per provider batch, with all-or-nothing rollback and one progress update.
- Reused generation-bound semantic campaign snapshots across first pages and
  concurrent callers, with single-flight construction, sliding cursor leases
  and a global accounted-byte cap.
- Added cooperative cancellation/deadline state, observable detached atomic
  completion, and checkpoints to semantic campaign scans, embedding batches and
  raw-ingest inventory/enqueue/finalize boundaries.
- Reduced runtime source-guard refreshes to bounded directory identity checks,
  coalesced idle watchdog status publications, added worker-start retry, and
  separated Operational Memory projection revision from unrelated database
  commits while retaining periodic physical attestation.
- Added a shared diagnostic snapshot for Doctor/health/readiness, a lightweight
  semantic-readiness envelope on retrieval consumers, exact surface-aware memory
  capabilities, and a formal `fact` mode with a deprecated `claim` alias.
- Aligned Gemini/Codex command safety and environment parity. Read-only MCP scan
  tools no longer acquire the canonical-meta file gate, while mutable tools and
  ordinary CLI diagnostics retain their bounded control-plane telemetry.
- Projection rebuild apply now shares the schema-maintenance lock with rollback,
  rejects active or malformed rollback receipts before any backup or projection
  write, and binds its database and projection roots across the guarded apply.
  Receipt classification is bounded and self-contained; completed rollback
  receipts remain terminal after later legitimate generation advances.
- Pending rollback recovery now hashes the live database before acquiring the
  Windows SQLite exclusive lock, then revalidates file identity, data version,
  schema, generations and projection under lock. This avoids a Windows
  `PermissionError` without weakening the no-writer compare-and-swap gate; CAS
  failures also retain phase, errno and WinError diagnostics.
- Split the bounded claim-graph node and edge ceilings so the canonical
  2,500-node projection can retain its governed degree-limited edge set. The
  edge surface remains fail-closed above 30,000 entries, and projection-v2
  singleton leaves may exceed 256 KiB only within the existing 1 MiB object
  ceiling; multi-entry leaves remain capped at 256 KiB. Root descriptors now
  require their exact component key set before closure traversal, so surplus or
  omitted components cannot hide behind a bounded descriptor read.
- Added a fail-closed compatibility path for expired pre-budget auto-ingest
  launch records that lack `reserved_tokens`: entries older than the complete
  rolling 24-hour window are validated and pruned, while any such active entry
  still blocks budget authorization instead of being assigned an inferred cost.

# Vector Lake 11.19.1

- Raised the default automatic-ingest safety ceiling to 100 tasks per hour and
  2,000 tasks per rolling 24 hours while retaining the 32,768-token per-task
  limit. Aggregate reservation ceilings now match those task ceilings at
  3,276,800 and 65,536,000 tokens respectively, and the bounded launch ledger
  accepts the full 2,000-entry window.
- Kept automatic ingest disabled and raw-text model processing unapproved by
  default. Budgeted normal tasks continue to use non-interactive, read-only
  Codex execution; privacy, lease, circuit-breaker, and policy-failure gates are
  unchanged.

# Vector Lake 11.19.0

## P1 architecture remediation

- Added schema v8 embedding metadata and provider-return generation CAS so stale
  provider responses cannot become current after a concurrent canonical update.
  Persisted validity follows the node input hash/model/dimension/contract instead of
  globally invalidating every vector on unrelated writes; bounded KNN expansion skips
  nearer legacy or stale rows without false-empty results.
- Added a completed-receipt-only v8-to-v7 schema rollback. Migration receipts now bind
  the exact pre-v8 projection pair; rollback creates a verified forward v8 recovery
  bundle, safely reuses verified complete or partial forward components after a
  pre-receipt crash and rebuilds only missing components, rejects unconfirmed
  post-migration writes, removes SQLite sidecars, and has fresh-process
  Doctor/search/MCP acceptance against the frozen 11.18.3 runtime.
- Bound the FTS projection to canonical generation, row count and corpus digest. Search
  now bypasses incomplete FTS, uses a committed bounded lexical fallback, and exposes
  shallow/deep health failures until a verified rebuild completes.
- Split mutation-outbox poison attempts from transient generation, lease and SQLite
  conflicts; deterministic payload failures still dead-letter, while transient races
  remain retryable across process restarts.
- Replaced unbounded operational-memory fallback with a source-row cap and stable
  retry contract, and added bounded Watchdog maintenance that automatically converges
  the derived FTS index. Search-index schema v6 now binds canonical rows, document
  mappings, and both physical FTS surfaces to a durable proof; connection revision
  caching keeps stable hot reads O(1), while equal-count tampering, lost pending work,
  integrity-limit breaches, and certification races fail closed and trigger bounded
  replay instead of returning a false-green empty result.
- Reduced MCP runtime-revision overhead with a single-flight TTL/metadata/full-hash
  guard, raised the bounded fast lane to 2 workers plus 4 queued calls, and added stable
  retry metadata for saturation. Strict per-call hashing remains opt-in.
- Added raw-inventory metadata fast paths, deterministic periodic content scrubbing and
  true trailing-edge Watchdog event debounce with maximum staleness and overflow repair.
- Added global backup inventory, quota/free-space telemetry and pre-staging capacity
  gates for maintenance and schema-migration backups.
- Added a generation-bound, read-only semantic-readiness campaign reporting exact
  evidence, extraction, current-assessment, source-integrity and topology debt. Its
  first snapshot now has hard row/JSON/graph/debt limits; cursor pages use a two-entry,
  120-second source-bound LRU and never repeat the full database/graph scan.
- Added a real read-only MCP surface; made merge suggestions preview-first; made failed
  Doctor and non-ready Readiness CLI reports return non-zero status.
- Aligned all plugin roots and skills with the enabled runtime, removed unsupported
  direct queue/legacy janitor scripts, corrected command and sync semantics, removed
  hidden-reasoning/fake-tool contracts, and made automatic raw-text model processing
  require explicit configuration consent.
- Declared the supported product boundary as a healthcare-digitalization, controlled
  single-user expert workstation rather than a multi-tenant enterprise service.

# Vector Lake 11.18.3

- Enable the fail-closed automatic ingest host through an explicit, pinned
  runtime policy while preserving bounded task and token budgets.
- Report a disabled automatic ingest component explicitly and surface a Doctor
  warning instead of presenting the autonomous closure path as merely idle.
- Document the distinction between scan/enqueue sync and the automatic
  claim/generate/finalize lifecycle.

# Vector Lake 11.18.2

- Accept the bounded historical `.gemini/MEMORY` to canonical `MEMORY`
  storage-URI normalization during merge recovery only when raw identity and
  all other preserved Source metadata remain unchanged.

# Vector Lake 11.18.1

- Complete projection rebuilds with a bounded topology refresh and verify the
  published pair is current before reporting success.

# Vector Lake 11.18.0

- Added fingerprint-confirmed, runtime-scoped unsupported-claim debt registration with a complete recoverable backup and transaction-time candidate revalidation.
- Added version-bound ClaimAssessment CLI and MCP surfaces so stale reviews cannot be attached to a changed claim.
- Added verified whole-artifact raw locators when precise segment metadata is unavailable, while missing source bytes remain explicitly unresolved.
- Extended evidence-foundation backfill to upgrade conservative unresolved or unverified placeholders without replacing reviewed locators.
- Added canonical-only lineage and source-integrity repair without inventing missing Wiki projections or extraction runs.
- Batched canonical prefetch and append-only Claim/Evidence version writes, reducing a 500-page repair transaction from minutes to seconds in the production-scale simulation.

# Vector Lake 11.17.0

- Added a repeatable local-only 12k+ corpus performance gate covering cold/warm index loads, serial and concurrent search, FTS fallback, exact-identity startup, throughput, errors, and RSS.
- Made the generation-scoped exact-identity index single-flight so concurrent cold requests no longer repeat the full O(N) build and allocation.
- Rate-limited repeated search-backend failure logs per backend while preserving degraded results and exposing the suppressed count in search telemetry.
- Added deterministic exact recall for canonical keys, entity IDs, titles, and aliases ahead of fuzzy and vector ranking, including ambiguity-preserving alias results.
- Added a read-only, versioned retrieval benchmark with dataset hashes and reproducible Precision, Recall, MRR, and nDCG metrics.
- Added a stable Vector Lake-native Agent memory protocol plus an optional fail-closed eight-tool MCP surface; governed writes still flow through the existing operational-memory mutation contract.
- Clarified that SQLite remains canonical, Markdown is a projection, and operational memory is a compiled Agent-facing read model rather than a second source of truth.
- Removed search-reader dependence on the projection publisher lock while retaining sidecar, digest, identity, and canonical-generation validation.
- Added an independent UTF-8 byte budget and failure-path timing telemetry for page search results.
- Made query embeddings adaptive: strong FTS candidate sets bypass the remote provider, while sparse lexical recall retains hybrid vector search and operators can force always-vector behavior.
- Removed schema DDL bootstrap from the interactive embedding hot path so its quota deadline remains bounded.
- Added bounded daily storage-growth baselines for database/WAL bytes, Claim/Evidence version rows and payload bytes, and maintenance-backup bytes; Doctor now reports deltas and configurable growth warnings.

# Vector Lake 11.15.0

- Added independently durable orphan-GC receipts that bind the approved fingerprint to the verified backup manifest and mutation outbox IDs; Doctor now detects missing or incomplete recovery evidence.
- Removed history-retention planning from orphan GC and rewrote active governance protection as one bounded queue scan, eliminating the correlated JSON scan from the GC hot path.
- Split synchronous MCP execution into independent fast-read and heavy-task lanes, with per-lane admission, queue-wait, and execution metrics.
- Added debounced post-projection topology refresh with a maximum staleness bound, target-side graph indexes, database-growth telemetry, and bounded search results with phase timings.
- Hardened the standalone watchdog entrypoint so it loads paired runtime roots from `.mcp.json` before importing Vector Lake.
- Made orphan-source debt explicitly P2 and added transactionally revalidated cleanup for removed or newly referenced sources, while protecting foreign, critical, and terminal governance records.
- Upgraded ingest handoff to v5: generated `Source_*` names now satisfy the strict filename contract, legacy active jobs rebuild their canonical identity and task packet, and stale raw/Source/target baselines automatically invalidate the old lease and re-enter the rebuild path.
- Added narrowly scoped mixed validation for ingest integration: new Source pages remain full-validated while updates to structurally valid legacy targets may use schema validation until purpose metadata is upgraded.

# Vector Lake 11.14.0

- Moved durable subagent task packets and ephemeral query scratch out of versioned plugin directories, and removed unrelated cross-connection writes from the canonical identity validation cache key.
- Reconciled terminal ingest debt against the effective normalized raw revision, blocked ambiguous owner sets, and revalidated owner uniqueness inside the apply transaction.
- Upgraded the SQLite migration contract to `PRAGMA user_version = 4`, adding a durable ingest task-packet cleanup ledger with schema and backup inspection coverage.
- Hardened ingest contract v4 across `sync -> worker -> claim -> finalize`: legacy jobs are migrated or retired, candidate manifests are bound outside prompt text, task packets are path/shape/content checked and lease-repaired, and finalization revalidates raw, canonical, projection, disposition, and fencing state.
- Added bounded MCP blocking execution with configurable workers, queue capacity, admission timeout, shutdown drain, runtime status, and fail-closed source-revision detection.
- Added preview-first, fingerprint-confirmed backup retention with verified restore-point protection; orphan-page GC now requires an exact current candidate fingerprint before deletion.
- Hardened raw candidate scoping, case-insensitive component exclusions, `privacy/Diary` traversal blocking, watcher subscription refresh, per-root retry backoff, and bounded ingest-debt progress.
- Aligned README operator guidance with the current type/Synthesis schema, `/sync` surface, ingest v4 handoff, real environment keys, maintenance confirmation rules, module map, validation command, and Doctor/readiness output semantics.
- Added read-only-preview ingest-job debt reconciliation with backups, lease-fenced CAS updates, replayable verified task-packet cleanup, missing-raw retirement, processed-job closure, current-hash requeue, and duplicate-current-identity supersession.
- Replaced placeholder graph analysis with bounded topology computation, deterministic communities, a global degree cap, and dirty-graph retrieval isolation.
- Added cascade tombstones for operational memory, Claim/Evidence versions, and entity identities when canonical pages are deleted.
- Added preview-first cleanup for generated memory artifacts and obsolete indexer community-naming work, preserving mixed-content and decision-scoped records.
- Decoupled change-set retention from orphan-page deletion, paired idempotency cleanup, hourly stale-ingest expiry, and observable watchdog-status failures.
- Added global evidence-foundation coverage to semantic readiness and SHA-256-pinned CriticalDecisionRegistry import receipts.
- Added verified SourceArtifact byte hashes, raw-source locators, deterministic ExtractionRun records, and explicit lineage/independence flags; missing sources now remain `unverified` instead of receiving placeholder hashes.
- Added append-only Claim/Evidence version tables and an entity-identity registry; rename operations persist the old entity ID in frontmatter.
- Added append-only ClaimAssessment, immutable schema/dialect registration, quality-evaluation runs, and EvidencePacket 1.1 export authorization for evidence text.
- Critical-decision references now require an active registry record accepted by a caller-provided verifier before automatic P0 ranking, and semantic readiness can be evaluated for one mapped decision scope.
- Corrected Timeline documentation: it is a governed, rebuildable knowledge projection, not a CBSS business Event Store.
- Added a read-only CBSS `EvidencePacket` export over canonical Claim/Evidence/Source records; evidence text remains opt-in and bounded.
- Split infrastructure health from semantic readiness so governance debt and claim validity are visible without changing the write gate.
- Added explicit governance priority and `critical_decision_refs` ordering, including read-time normalization for legacy queue rows.
- Added CBSS boundary contracts for claim acceptance, critical-decision registry, business events, and semantic readiness.
- Reduced deep projection-check cost by deriving canonical versions from entity-only page extraction instead of rebuilding all claim and evidence records.
- Removed stale authority-source, module-map, fixed-test-count, and fixed-runtime-count claims from operator documentation.

# Vector Lake 11.13.0

- Ingest Subagent 领取协议增加 owner/token/generation fencing，最终完成使用事务内 CAS。
- Timeline 由 Claim 事务增量维护，查询使用稳定事件 ID 校验投影并在漂移时回退 canonical。
- Outbox 合并索引批次、抑制受管投影自写事件，并优先于旧文件事件队列执行。
- Embedding 使用 SQLite 跨进程滚动 RPM/TPM 窗口；索引重建和增量索引不再调用外部 API。
- 修复 GC page_key、Operational Memory 赢家删除、System 节点冷/热漂移和 payload 沙盒边界。
- 新增 Windows Python 3.13 CI；本地回归基线为 110 项测试。

# 🚀 Vector Lake 综合更新草案 (合并 Jules PRs)

以下是将 google-labs-jules 提交的多个针对性能和安全相关的 Pull Requests 内容进行**合并处理**后，生成的最终综合更新说明（Release Draft）：

## ⚡ 性能优化 (Bolt)：全面加速 YAML 解析与写入
**关联的 PRs**: #111, #110, #107, #106, #104

💡 **改动内容 (What)**: 
我们在核心层新增了 `yaml_utils.py` 模块，用于透明且动态地加载 LibYAML 的 C 扩展 (`CSafeLoader` 和 `CSafeDumper`)。在 `indexer.py` 及 `wiki_utils.py` 等处理海量 Markdown 文件的关键路径中，原有的纯 Python 库（`yaml.safe_load` / `yaml.dump`）已被底层的 `load_yaml` 和 `dump_yaml` 函数替代，并带有优雅降级机制（若环境中未安装 C 扩展，则安全回退到纯 Python 实现）。

🎯 **优化原因 (Why)**:
Vector Lake 的底层架构强依赖于从成百上千个 Markdown 文件中解析 YAML frontmatter。每当生成索引、执行数据湖审查 (Linting) 或别名修复时，纯 Python 层的 YAML 处理速度就成为了系统不可忽视的 O(N) 性能瓶颈。

📊 **业务影响 (Impact)**: 
得益于底层 C 扩展绑定的介入，我们在解析和写入大批量 YAML 元数据时的速度实现了质的飞跃（加载提速约 **8~10 倍**，写入提速约 **5~6 倍**）。这极大地缩短了 `python3 vector_lake/indexer.py` 重建知识图谱、以及各类维护脚本所需的执行时间。

---

## 🛡️ 安全修复 (Sentinel)：彻底消除图谱可视化组件中的 XSS 漏洞
**关联的 PRs**: #109, #108, #105, #86

🚨 **严重程度**: 严重 (CRITICAL / HIGH)

💡 **漏洞详情 (Vulnerability)**:
在拓扑图谱可视化引擎中，发现了两处跨站脚本攻击 (XSS) 漏洞：
1. **服务端 XSS** (`vector_lake/tool_graph.py`): 在将 Python 字典序列化为 JSON 字符串并直接嵌入到 HTML 模板的 `<script>` 标签块（`%%GRAPH_DATA%%`）时，未对特殊的 HTML 字符进行转义。
2. **DOM-based XSS** (`templates/topology.html`): 用户可控的 Markdown 变量（如 `node.name`, `node.group`）在未经清洗的情况下被直接通过 `.innerHTML` 插入到页面的 DOM 树中。

🎯 **潜在威胁 (Impact)**:
攻击者可以通过构造带有恶意 Payload 的节点或 Claim（例如包含 `</script><script>alert(1)</script>` 或 `<img src=x onerror=...>`）。当普通用户查看该图谱时，恶意脚本将突破原始标签上下文并在受害者浏览器中执行，可能导致敏感信息被盗或会话被劫持。

🔧 **修复方案 (Fix)**:
- **服务端**: 在 `json.dumps` 之后加入了链式替换规则，将所有的 `<`、`>` 以及 `&` 字符彻底转义为其对应的 Unicode 格式表示（例如 `\u003c` 等），杜绝了任何逃逸出 `<script>` 环境的可能。
- **DOM 层**: 引入了严格的 `escapeHTML` 辅助函数，确保所有动态生成的字符串在执行 `.innerHTML` 挂载之前得到安全清理；此外，对所有动态拼接的 `href` 属性包裹了 `encodeURI()`。

✅ **验证测试 (Verification)**:
所有修复均已通过 Playwright 自动化注入脚本的黑盒测试，恶意的测试节点（带有各种 XSS vector）目前被作为普通文本安全地呈现，前端未再触发任何意外的 JS 执行。相关的 Lint 与编译校验检查均已通过。
