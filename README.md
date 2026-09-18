# Vector Lake

Vector Lake 是一个本地文件优先的知识编译器。它不是传统向量库，也不是一次性 RAG 后端，而是把原始材料持续编译成可审计的 Markdown wiki，并同步生成面向 Agent 的结构化运行态记忆。

当前架构边界：

- `MEMORY/raw`：原始信源层，只读输入。
- `MEMORY/wiki`：人类可读的 Markdown 发布层，用于审计、浏览、复盘和长期资产沉淀。
- `MEMORY/wiki/index.json`：页面级运行索引，用于搜索和拓扑扩展 (基于 BM25)。
- `MEMORY/wiki/.meta/vector_lake.db`：统一的 SQLite 底层引擎，不仅保存实体 (Entities)、断言 (Claims)、证据 (Evidence)、信源 (Sources)、图拓扑、变更集和治理队列，同时也作为 Agent 运行态记忆层，把 `Claim` 编译为 `fact / preference / decision / task_state` 存入 `operational_memory` 表。
- `MEMORY/purpose.md`：版本化战略控制面。YAML 契约驱动摄取范围、证据等级、意图权重、SIR 复审和张力合成阈值；营销噪音与范围外资料不进入主图谱，但保留最小丢弃审计。`purpose_vectors.json` 仅保留为旧版回退，不再是权重主源。

如果 `MEMORY/wiki/.meta` 不可写，运行时会回退到仓库内 `data/v8_meta/`。

## 已知限制与运维要求 (Known Limits & Operational Requirements)

以下约束由当前实现决定，部署前必须满足，否则会出现与预期不符的行为。

| 约束 | 事实 | 规避 |
|---|---|---|
| 必须常驻守护进程与摄取 Runner | outbox 消费、增量索引、定时 lint 均在 `watchdog_sync.py` 内；摄取任务包的**模型调用**在宿主侧 `scripts/ingest_runner.py`（默认 shadow，只报告不写页面），由 `scripts/ingest_runner_service.py` 负责重启。MCP-only 模式没有消费者，写入会持续堆积；只跑 watchdog 时任务包会停在 `awaiting_subagent` | 生产环境常驻 `watchdog_sync.py` 与 `ingest_runner_service.py`；仅做只读检索时才可省略 |
| Runner 健康默认只告警 | `runner_absent` / `runner_stalled` / `runner_failing` 默认进 `warnings`，不翻转 `ok`；“从未跑过”与“跑挂了”由 `.meta/runtime/runner_supervisor.json` 区分 | 需要把 Runner 缺失升级为硬门时设 `VECTOR_LAKE_RUNNER_STRICT=1` |
| 编译依赖 LLM 宿主 | `cli.py sync` 只产出 subagent 任务包，不自行编译；`native_llm.generate_text` 恒抛 `SubagentTaskRequired` | 在具备 subagent 能力的宿主内运行摄取流程 |
| 向量检索需要显式回填 | 任何页面写入都会使该节点向量失效；无自动重嵌 | 定期 `python cli.py embedding-backfill --apply`；需 `GEMINI_API_KEY`（无 key 时 `search` 输出 `[DEGRADED]` 横幅） |
| GC 的孤儿判据是拓扑度数 ≤ 1 | 度数来自 canonical 的 `links` / 共享来源 / claim 共现；**不是**可视化边集 | 先 `python cli.py gc` 做 dry-run，它会在同一调用中打印每个页面的实际度数。单次删除超过候选页 50% 时会自动中止，需 `--force` 才继续 |
| 删除类命令默认演练 | `gc` / `delete` 默认 dry-run，必须 `--apply` 才落盘 | 保持默认；仅在确认 dry-run 输出后追加 `--apply` |
| 修复类工具需要后置重建 | `wiki-restore` 会恢复 Markdown，但索引投影需单独重建 | 按其输出末尾提示运行 `projection-rebuild-index --apply` |
| 全量重建成本随语料线性增长 | 冷启动需对全部正文分词（5000 页约 100 秒）；未变更节点会被跳过 | 仅在有结构变更时全量重建；日常依赖增量更新 |
| 人工编辑会经过校验 | 未通过 schema / 目的契约校验的手改页面会被拒绝并保留原文件，日志给出原因 | 修复页面后重新保存，或查看守护进程状态文件的 `current_action` |

## Architecture

```mermaid
graph LR
    RAW["MEMORY/raw<br>Immutable sources"] --> INGEST["Native Subagents<br>Asynchronous Ingestion Pipeline"]
    INGEST --> WIKI["MEMORY/wiki<br>Markdown pages"]
    WIKI --> INDEX["index.json<br>page index + BM25"]
    WIKI --> META["vector_lake.db<br>SQLite Canonical Store"]
    META --> CLAIM["SQLite claim_graph_edges<br>governed topology"]
    META --> MEMORY["SQLite operational_memory<br>agent runtime memory"]
    MEMORY --> PACKET["Memory Packet<br>selective context injection"]
    INDEX --> QUERY["search<br>Local Expansion + BM25 + Graph Spreading"]
    CLAIM --> QUERY
    PACKET --> QUERY
```

核心原则：**Markdown 是人类界面，`.meta` 是事实底座，`operational_memory` 是 Agent 运行层。**

## 📂 受控类型与文件结构规范 (Controlled Types & File Structures)

为了保持图谱检索的高信噪比与一致性，Wiki 目录下的 Markdown 文件必须遵循严格的**受控命名前缀**，并被划分为两种完全不同的文件组织结构规范。

### 1. 核心受控类型 (Prefixes)

`validate_wiki_filename` 强制以下前缀（禁用空格与非规范符号，格式如 `Institution_北京协和医院.md`），并额外限制正则 `[Type]_[MainName](-[SubName])*.md`、总长 ≤ 120：

- **`Institution_*`**：医疗机构、医院、医学院及科研院所实体。
- **`Vendor_*`**：商业侧供应商、IT 企业、设备厂商。
- **`Product_*`**：医疗 IT 产品、系统、软件架构（强制包含资质合规槽位）。
- **`Person_*`**：核心高管、研究员、关键人物。
- **`Event_*`**：重要会议、行业突发事件。
- **`Concept_*`**：抽象架构、理论、业务机制。域总览页沿用 `Concept_Overview_<domain>.md` 形式（由 `scripts/compile_domain_overviews.py` 生成）。
- **`Policy_*` / `Standard_*`**：政策法规、行业标准。
- **`Source_*`**：`raw/` 原始信源的一对一摘要节点。
- **`Synthesis_*`**：推演、跨界比较与调研长文。
- **`System_*`**：系统投影页（社区页、总览等）。前缀校验直接放行，且不参与 purpose 契约门。

`index.md`、`log.md`、`overview.md`、`orphan_pages.md`、`wiki_link_stats.md`、`Synthesis_log.md` 为白名单文件，不走上述命名与校验规则。

### 2. 双重文件结构设计 (Dual-Schema Format)
根据文件的受控类型，内部的 Markdown 结构被严格限制为两类：

#### A. 实体与概念类 (Dual-Schema Mandate)

- **适用类型**（即 `schema_validator.VALID_H3_SLOTS` 中定义固化插槽的类型）：`Vendor_`, `Product_`, `Person_`, `Event_`, `Concept_`, `Policy_`, `Standard_`, `Institution_`
- **结构要求**：物理上由 `---` 分隔为两部分：
  1. **`## 1. 编译事实 (Compiled Truth)`**：Read Model，只保留当前共识。特征点必须落在类型专属的 `###` 固化插槽内（例如 Vendor 的 `### 组织架构与商业模式`），插槽名不匹配会被 `schema_validator` 拒绝。
  2. **`## 2. 证据时间线 (Evidence Timeline)`**：Event Store，只能追加。格式形如 `- [YYYY-MM-DD] [Event_Tag] ...`。

#### B. 豁免类 (Free-Form)

- **适用类型**：`Source_`, `Synthesis_`
- **结构要求**：自由格式，不切割“事实 / 时间线”，用于单篇文献精读、书籍伴读笔记与横向战略研报。

## Quick Start

> **运行前提**：Vector Lake 不是自包含的编译器。原始信源到 Wiki 页面的“编译”由 LLM 宿主（subagent）执行，`cli.py sync` 只负责生成任务包。因此实际运行需要同时满足：**① 单机 ② 常驻 `python watchdog_sync.py` ③ 常驻摄取 Runner（`scripts/ingest_runner_service.py`）④ 具备 subagent 能力的宿主**。只启动 MCP server 而不启动守护进程时，写入会在 5 分钟后进入 outbox 积压告警状态。

1. **环境配置**：`config.json` 的 `target_directories` 留空即表示使用当前 `MEMORY/raw/`（多机可移植）；也可显式填写绝对路径。`supported_extensions` 配置允许扫描的后缀。非 embedding 文本推理不由插件直接调用外部 API；需要推理的后台任务会生成当前环境 subagent 任务包。`GEMINI_API_KEY` 只影响 embedding 与混合检索的向量分支。
2. **生成编译任务**：执行 `python cli.py sync` 得到原始信源的 subagent 摄取任务包，由宿主执行并回填。也可由 `watchdog_sync.py` 在检测到 raw 变更时自动生成。
3. **后台监听与自治管理**：运行 `python watchdog_sync.py` 启动守护进程（它会接管 outbox 消费、增量索引与定时 lint）。即使不使用增量监听，只要发生写入就需要它来消费 outbox。它搭载的核心基建与防御系统：
   - **双轨看门狗 (Two-Track Watchdog)**：除增量文件外还捕获 `on_deleted` / `on_moved`，因此重命名或删除页面不会在图谱里留下幽灵节点。
   - **写入健康门 (Write Health Gate)**：写入只在**硬故障**下被阻断（数据库不可用、存在 hard-failed 的 `mutation_outbox` 行）。outbox 积压超过 `VECTOR_LAKE_OUTBOX_MAX_BACKLOG`、投影漂移、心跳过期、终态失败作业、时间线 parity 漂移都属于**可修复降级**，只记录告警并继续写入——阻断它们会同时阻断唯一的修复通道。需要严格模式的运维方可分别用 `VECTOR_LAKE_OUTBOX_BACKLOG_BLOCKING` / `VECTOR_LAKE_TERMINAL_FAILED_JOBS_BLOCKING` / `VECTOR_LAKE_TIMELINE_PARITY_BLOCKING` 把这些降级提升为阻断。
   - **I/O 批处理防抖 (I/O Debouncing)**：同批次修改合并为一次 `index.json` 写盘；`index.json` 不再保存完整正文（每个节点保留至多 320 字符的摘要；实测 7125 节点 / 14.45 MB，摘要占约 15.8%，`weighted_edges` 占 22%），投影写入使用短事务，全量重建不再冻结数据库。
   - **两步思维链摄入 (Payload-Based MCP)**：Agent 先输出分析缓冲（Tension / Consensus / Unknowns），长文本经 `payload_file` 落盘后入湖，规避 CLI 传参截断与 JSON 解析失败。
   - **语义张力量化模型 (STQM)**：图谱原生支持 `tension_edges`，把争议与矛盾结构化为冲突边，Query 时可直接展示领域盲区。
   - **跨类型本体拦截 (PIEA)**：入口级跨类型查重，避免同一名称多态共存；内置正则清洗违规嵌套前缀（如 `Concept_Synthesis_`），并由 schema gate 校验受控前缀与类型。
   - **持久化增量索引与稀疏图遍历 (Sparse Graph Traversal)**：前台变更先写 durable outbox，Watchdog 合并批次后更新索引；`_calculate_weighted_edges` 使用稀疏遍历并限制每节点投影边数。
   - **跨平台 I/O 韧性 (I/O Resilience)**：后台子脚本拉起时注入 `PYTHONIOENCODING=utf-8`，避免中文 Windows 上的编解码崩溃。
   - **定时确定性维护 (Scheduled Deterministic Maintenance)**：每天 10:00 与 23:00 刷新脏图拓扑、执行只读 lint，并做 SQLite WAL checkpoint。研究、去重、聚类等独立脚本不会被该循环隐式启动。
   - **向量投影存于 SQLite (vec_embeddings)**：向量由 `sqlite-vec` 存放于 `vector_lake.db` 的 `vec_embeddings` 表，不再依赖模型侧的 JSON 载荷；语义去重守护进程优先读取该表，仅在缺失时回退到 `embeddings.pkl` 旧缓存。
   - **本体免疫型排重 (Ontology-Immune Deduplication)**：去重守护进程豁免 `Source_*` 等时序不可变信源，避免“相似度过高即合并”把不同日期的研报强行合流。
   - **统一 SQLite 数据底座 (Unified SQLite Engine)**：实体、断言、证据、信源、图拓扑、变更集、治理队列与运行态记忆统一落在 SQLite，启用 WAL。
   - **差分垃圾回收机制 (Diff-based GC)**：Markdown 层面重命名 / 删除或断言被移除时，同步层按页面增量清理对应的实体、断言与证据，不再只增不减。
   - **夜间拾荒者集群 (Janitor Swarm)**：语义去重的**分片准备器**。`python scripts/launch_janitor_swarm.py` 读取治理队列中的 pending merge 项，按 `SHARD_SIZE` 切分为子代理任务包并写出 `janitor_manifest.json`。**它不会自行合并或启动任何外部进程**；实际合并由宿主子代理调用 `resolve_governance_item` 或 `bulk_reconciliation` 完成。
   - **MCP 载荷沙箱 (Payload Sandbox)**：所有长文本参数经 `payload_file` 指向的文件传入，读取受 `VECTOR_LAKE_PAYLOAD_ROOT`（或 `brain/<run>/scratch/`）与 `VECTOR_LAKE_PAYLOAD_MAX_BYTES` 限制，避免命令行传参截断与注入。
   
### 日常运行入口

1. **常驻守护**：`python watchdog_sync.py`（outbox 消费、增量索引、定时 lint 与 WAL checkpoint 都在这里；只跑 MCP server 会让写入持续积压）。
2. **常驻摄取 Runner**：`python scripts/ingest_runner_service.py --limit 1 --interval 180 --model-cmd "python scripts/ingest_model_pi_subagents.py"`。Runner 认领任务包、在子进程里调用宿主模型，再经 `finalize_ingest` 提交；不加 `--no-shadow` 时只报告 `needs-model`，不写页面。
3. **检索**：`python cli.py search "<keyword>"`，或 `python cli.py query "<question>"` 走预算受控的上下文组装。
4. **摄取队列**：`python cli.py ingest-tasks` 查看 queued / awaiting_subagent 作业；宿主 subagent 完成后经 `finalize_ingest` 入湖。
5. **周期治理**：`python cli.py review` 处理冲突与候选队列，`python cli.py doctor` 检查运行健康度。

历史版本的逐项特性说明不再在本文件维护；版本变更请查 `CHANGELOG.md`，运行契约以本节与上方“已知限制”表为准。

## Operational Memory

运行态记忆由 `vector_lake/governance_store.py` 从 canonical claims 编译生成。它解决的问题是：Agent 常常只需要一个事实、偏好、决策或任务状态，不应该每次加载整页 Markdown。

> **"Wiki-as-Database" 写回范式**：Agent 在运行态生成的新记忆，**严禁**直接写入 SQLite。它们必须通过 `update_operational_memory` 工具，按严格的 **Dual-Schema（双架构）** 规范，即 `# 1. 编译实体特征 (Compiled Truth)` 与 `## 2. 证据时间线 (Evidence Timeline)`，物理追加到相应的 Wiki 实体文件（如 `Concept_UserPreferences.md`）的时间线下方。这确保了在图谱完全重建时，Agent 记忆依然通过 Markdown 原质保留。

内置类型：

- `fact`：一般事实或断言。
- `preference`：用户偏好、默认策略、首选路径。
- `decision`：已批准或当前有效的决策。
- `task_state`：任务状态、阻塞项、待处理事项。

每条运行态记忆会计算：

- `confidence_score`
- `freshness_score`
- `authority_score`
- `importance_score`
- `reinforcement_score`
- `validity_factor`
- `memory_score`

冲突规则：

- 显式 contradiction：`authority_score > confidence_score > updated_at`。
- 同一 `memory_key` 的 `preference / decision / task_state`：`updated_at > authority_score > confidence_score`。
- 失败侧标记为 `superseded`；无法裁决时保留 `conflicted`。

`query` 会优先生成 Memory Packet，再按预算拼接相关 wiki 页面。Memory Packet 包含当前偏好、决策、任务状态、相关事实、冲突/陈旧告警和证据指针。

## Storage Layout & Architecture

Vector Lake adopts a hybrid CQRS-like architecture with O(1) incremental native SQLite syncing.

- **Markdown (Source of Truth)**: `wiki/*.md` and `raw/*.md`.
- **Database (Read Model & Fast Mutations)**: `vector_lake.db` containing unified SQLite entities, claims, graph edges, and operational memory.
- **Concurrency & Atomicity**: 多页变更经 `MutationCoordinator` 在单个 `BEGIN IMMEDIATE` 事务内提交 canonical 状态与 `mutation_outbox` 意图，再materialize Markdown 投影；事务失败整体回滚，投影失败由 outbox 重试。**Wiki 页面本身没有自动 `*.bak` 备份**——删除类命令（`gc` / `delete`）会先写恢复点目录（`backup/gc/`、`backup/delete-source/`），其余写入依赖 canonical 状态重建。数据库副本写在 `.meta/backups/`（`vector_lake_<ts>.db.bak` 及 `-wal` / `-shm` sidecar），其总量由 `backup-retention` 约束。

```text
MEMORY/
  purpose.md          <-- Versioned Strategic Purpose Contract & Epistemic Stance
  raw/
  backup/
    gc/               <-- Recovery points written before gc deletes a page
    delete-source/    <-- Recovery points written before a cascade delete
  wiki/
    *.md
    index.json
    .meta/
      purpose_vectors.json <-- Optional legacy fallback for intent weights
      vector_lake.db       <-- Unified SQLite Store (Entities, Claims, Graph, Timeline, Operational Memory)
      backups/             <-- Bounded SQLite copies (.db.bak + -wal / -shm sidecars)
      runtime/             <-- Runner / supervisor / lock-contention status JSON
      subagent_tasks/      <-- Native-subagent task packets
```

## Commands

> **Note**: Vector Lake 现已全面接入 MCP (Model Context Protocol)。大语言模型 Agent 将直接通过 `vector_lake/mcp_server.py` 调用底层 Tool 接口，不再需要通过终端模拟。
> 
> **Slash Commands**: 本仓库**不随附**任何 slash command 兼容层，`commands/` 目录已不存在。所有能力通过 MCP 工具面调用；打包技能的宿主可另用 `$vector-lake:query`、`$vector-lake:timeline` 同名技能。
>
> 仅可通过 MCP 工具或 CLI 调用（无 slash command 层）：
> - `sync_vector_lake`：自动调度 Ingestor 子智能体执行图谱知识的异步增量同步
> - `review_governance_list`：检查统一治理队列
> - `resolve_governance_item`：处理治理队列中的待办项
> - `trigger_audit_graph`：合成图拓扑并执行审查
> - `get_governance_debt`：查看图谱治理债务指标
> - `lint_vector_lake`：执行节点健康度自愈审查（支持 `auto_fix=True` 自动修复残缺元数据与图谱断层）
> - `trigger_autonomous_research`：自主扫描并下发网络检索指令
> - `visualize_vector_lake`：直接生成并刷新 3D 可视化拓扑面板
> - `doctor_vector_lake`：运行环境与依赖健康体检
> - `gc_vector_lake`：垃圾回收与孤儿节点自动清理（默认 dry-run）
> - `delete_source`：级联删除信源与切断图谱边（默认 dry-run）
> - `trace_vector_lake`：展示实体或知识断言的溯源追踪
> - `merge_suggestions_vector_lake`：扫描并提出知识合并建议
> 
> 以下底层 CLI 命令仍然保留，供人类开发者日常手动调试与状态维护。

基础体检：

```powershell
python cli.py doctor
```

编译 raw sources：

```powershell
python cli.py sync
```

启动后台守护进程（增量监听）：

```powershell
python watchdog_sync.py
```

搜索页面层：

```powershell
python cli.py search "Agent memory" --top_k 5
```

搜索运行态记忆：

```powershell
python cli.py search "部署目标" --mode memory --top_k 5
```

搜索 claim-level facts：

```powershell
python cli.py search "Agent memory" --mode claim --top_k 5
```

基于 Memory Packet 和 wiki 证据做 synthesis：

```powershell
python cli.py query "对比 Karpathy LLM Wiki 与 Agent memory 的架构差异"
```

只预览 query 输出，不落盘：

```powershell
python cli.py query "总结当前运行态记忆架构" --dry-run
```

治理与审计：

```powershell
python cli.py review
python cli.py review resolve <index|item_id> --resolution skip
python cli.py audit-graph
python cli.py lint
python cli.py lint --auto-fix
python cli.py research
python cli.py debt --top 20
python cli.py trace "<query-or-id>"
python cli.py merge-suggestions --limit 20
```

图谱与清理：

```powershell
python cli.py graph
python cli.py gc --days 30 --dry-run
python cli.py delete "<raw-source-path>" --dry-run
```

投影与 canonical 维护：

```powershell
python cli.py projection-report --limit 20
python cli.py canonical-backfill --limit 100
python cli.py canonical-backfill --apply --limit 100
python cli.py timeline-rebuild --apply
python cli.py timeline-repair
python cli.py timeline-repair --apply
python cli.py projection-rebuild-index --apply
python cli.py embedding-backfill --limit 200
python cli.py embedding-backfill --apply --limit 200
python cli.py wiki-restore --apply --limit 10
python cli.py gram-index
python cli.py gram-index --if-due --apply
```

备份保留与幂等性键维护：

```powershell
python cli.py backup-retention
python cli.py backup-retention --keep 2 --max-bytes 6442450944 --apply
python cli.py idempotency-status
python cli.py repair-idempotency --table mutation_outbox
python cli.py repair-idempotency --table mutation_outbox --apply
```

这些维护命令默认以 dry-run 或显式 `--apply` 分离执行。`canonical-backfill` 只从已有 Wiki Markdown 回填 SQLite canonical；`projection-rebuild-index` 只从 canonical 重建 `index.json`、FTS 和 `claim_graph.json`，并保留已有 `vec_embeddings`；`embedding-backfill` 按 RPM/TPM 限额断点补齐缺失向量；`wiki-restore` 只把 canonical-only 记录恢复为缺失的 Markdown 投影；`timeline-repair` 就地补齐 `timeline_events` 的 parity 漂移，不重建整表；`gram-index` 报告或重建运行态记忆检索用的精确 n-gram 倒排，`--if-due` 只在该索引确实落后时才重建（见下，`--compact` 已随增量机制一并删除）。

`gram-index` 的重建节奏：索引被写入后就不再精确，而是带着陈旧基表继续服务，因此读路径上的搜索会退回精确扫描（实测约 0.745 s，对照索引路径 0.295 s）。让索引重新精确只有重建一条路，代价是活库上约 430 s，且**期间拒绝所有写入**。所以重建只发生在维护位置：守护进程的定时维护块（在 WAL checkpoint 之前）与 `gram-index --if-due --apply`。**后者需要守护进程在运行**：没有守护进程时（如本机现状）不会有任何自动触发，只能由人按 `due=` 手工执行——`doctor` 的 `Watchdog Status` 是判断这一点的依据。阈值为 `REBUILD_AFTER_WRITES = 500`，按文档数计，而不是按事务或时长；选择它的依据不是「搜索省下的时间何时回本」（实测约 1200 次搜索），而是「重建能让写入停多久」——因此宁少勿多。`due=` 与 `of 500` 就是这个欠账的当前值，而不是故障。

`backup-retention` 约束 `.meta/backups`（SQLite 副本目录，与 `MEMORY/backup/` 下的页面恢复点无关）：默认保留最新 3 份副本，其余受 12 GiB 预算约束，**最新一份无论是否超预算都不会被删**；`idempotency-status` 报告各幂等表当前达到的唯一性等级，`repair-idempotency` 清除冗余幂等键以便建成完整唯一索引——它两种模式下都不删除业务行。

## Config

`config.json` 与环境变量共同控制运行范围和模型调用。该文件是机器相关配置，**不入 git**：克隆后执行 `cp config.example.json config.json` 再按本机填写。文件缺失时使用代码内置默认值（含默认排除列表 `exclude_paths`），不会退化为“无排除”。

- `target_directories`：raw source 扫描路径。
- `exclude_paths`：排除目录。
- `supported_extensions`：当前启用的输入扩展名。
- `memory_dir`：MEMORY 根目录（机器相关，按安装填写）；可用 `VECTOR_LAKE_MEMORY_DIR` 覆盖。
- `processed_files_path`：**legacy 字段，当前代码不读取**。已处理 raw 文件记录存放在 SQLite `processed_files` 表。
- `VECTOR_LAKE_DB_PATH`：覆盖 SQLite 数据库路径（默认 `<MEMORY>/wiki/.meta/vector_lake.db`）。
- `VECTOR_LAKE_PAYLOAD_ROOT` / `VECTOR_LAKE_PAYLOAD_MAX_BYTES`：MCP `payload_file` 沙箱的可读根与单文件字节上限（默认 5 MiB）。
- `VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE=1`：跳过写入前健康门（仅用于受控维护，不建议常开）。
- `VECTOR_LAKE_EMBEDDING_RPM` / `VECTOR_LAKE_EMBEDDING_TPM`：embedding 调度限额，默认分别为 `3000` 和 `1000000`。
- `VECTOR_LAKE_EMBEDDING_UTILIZATION`：安全水位，默认 `0.8`，即按 2400 RPM / 800k TPM 调度。
- `VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS` / `VECTOR_LAKE_EMBEDDING_MAX_BATCH_TOKENS`：单批条数与 token 上限，默认 `100` / `200000`。
- `VECTOR_LAKE_EMBEDDING_TIMEOUT_MS`：单次 embedding HTTP 超时，默认 `30000` 毫秒。
- `VECTOR_LAKE_TOKENIZER`：强制分词后端，取 `jieba` 或 `rjieba`；不设置则自动优先 `rjieba`。指定的后端不可用时只会告警并回退，不会禁用分词。
- `VECTOR_LAKE_OUTBOX_MAX_BACKLOG`：outbox 待处理行数阈值，默认 `2000`。**超出后默认只计为降级告警，不阻断写入**（阻断积压等于掉断唯一的自愈路径）；设 `VECTOR_LAKE_OUTBOX_BACKLOG_BLOCKING=1` 才升级为阻断。
- `VECTOR_LAKE_OUTBOX_FAILURE_NONBLOCKING=1`：把 `mutation_outbox_failed` 从硬故障降为降级告警。默认关闭，因为失败行会阻塞 canonical 写入；打开等于用可用性换安全性。
- `VECTOR_LAKE_TERMINAL_FAILED_JOBS_BLOCKING=1` / `VECTOR_LAKE_TIMELINE_PARITY_BLOCKING=1`：把终态失败作业与时间线 parity 漂移从降级升级为阻断写入的诊断用闸门，默认关闭。
- `VECTOR_LAKE_BACKUP_KEEP` / `VECTOR_LAKE_BACKUP_MAX_BYTES`：`backup-retention` 的默认保留份数（`3`）与字节预算（`12 GiB`）。
- `VECTOR_LAKE_RUNNER_EXPECTED=0`：不再把缺失的摄取 Runner 报为告警；用于不需要摄取的主机。
- `VECTOR_LAKE_RUNNER_STALE_SECONDS`：Runner / 监督器心跳过期阈值，默认 `2400` 秒。
- `VECTOR_LAKE_RUNNER_STRICT=1`：把 Runner 相关告警提升为降级。默认只告警，不影响 `ok`。
- `VECTOR_LAKE_RUNNER_MODEL_CMD`：Runner 的模型接缝命令，等价于 `--model-cmd`。相关脚本另读 `VECTOR_LAKE_RUNNER_PI_BIN`、`VECTOR_LAKE_RUNNER_SUBAGENT_AGENT`、`VECTOR_LAKE_RUNNER_MODEL_TIMEOUT`、`VECTOR_LAKE_RUNNER_COOLDOWN`。
- `VECTOR_LAKE_RERANK_WEIGHT`：检索 Phase-2 重排的权重，默认 `0.4`，即 `0.6 × 上游归一化分 + 0.4 × bm25s 词汇分`；设为 `0` 可完全恢复旧排序。
- `VECTOR_LAKE_LEIDEN_L1_RESOLUTION` / `VECTOR_LAKE_LEIDEN_L0_RESOLUTION`：Leiden 的 Micro / Global 分辨率，默认 `2.0` / `1.0`。分辨率越高社区越小。
- `VECTOR_LAKE_LEIDEN_SEED`：Leiden 随机种子，默认 `42`。**必须固定**才能保证社区划分可复现。
- 所有进程通过 SQLite 滚动窗口共享 RPM/TPM 预算；索引重建和增量索引不调用 embedding API，内容变更后的旧向量由显式 `embedding-backfill` 补齐。

其余开关（凡 `vector_lake/` 里出现的 `VECTOR_LAKE_*` 字面量都在此登记；由测试守往）：

- `VECTOR_LAKE_EMBEDDING_MODEL`：embedding 模型名（默认见 `embedding_scheduler.DEFAULT_MODEL`）。
- `VECTOR_LAKE_EMBEDDING_DIMENSION`：向量维度（默认见 `DEFAULT_DIMENSION`）。
- `VECTOR_LAKE_EMBEDDING_MAX_CHARS_PER_ITEM`：单条目字符上限，默认 `15000`。
- `VECTOR_LAKE_EMBEDDING_MAX_TOKENS_PER_ITEM`：单条目 token 上限，默认 `7500`。
- `VECTOR_LAKE_EMBEDDING_MAX_RETRIES`：单次重试上限，默认 `5`。
- `VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES`：连续失败批次上限，默认 `3`。
- `VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS`：embedding run 的租约过期时间，默认 `3600`（下限 `60`）秒。
- `VECTOR_LAKE_MEMORY_SEARCH`：运行态记忆检索后端，默认 `gram`（精确 n-gram 倒排）；其他取值回退到投影扇描；`legacy` 强制旧路径。
- `VECTOR_LAKE_IGNORE_SCHEDULED_LINT_STATE`：设值（非空）时忽略已完成的定时 lint 标记，用于强制重跑一次。
- `VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS`：outbox 最老待处理行的年龄阈值，默认 `300` 秒。
- `VECTOR_LAKE_WRITE_LOCK_CONTENTION_WINDOW_SECONDS`：判定写锁争用是否仍属「当前」的滑动窗口，默认 `600` 秒。
- `VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS`：等待中 subagent 作业的条数阈值，默认 `500`。
- `VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS`：等待中 subagent 作业的年龄阈值，默认 `86400` 秒。
- `VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING=1`：把 subagent 积压从降级告警升级为阻断写入。默认关闭。
- `VECTOR_LAKE_SUBAGENT_RUN_ID`：本进程作为 outbox / job 租约持有者的标识；不设置时用 `hostname:pid`。
- Ingest 完成必须提交领取阶段返回的 `job_id`、`lease_owner`、`lease_token` 和 `lease_generation`；过期 Worker 的结果会被最终 CAS 拒绝。

### 依赖与分词后端 (Dependencies & Tokenizer)

必需依赖见 `requirements.txt`；已验证版本的直接依赖钉版见 `requirements.lock.txt`。

#### 社区检测：Leiden（已取代 Louvain）

`python-louvain` 已移除，改为 **`igraph` + `leidenalg`**。实现在 `scripts/community_clustering_daemon.py`：

- Louvain 用 dendrogram 层级，Leiden 用 `resolution_parameter`，因此两个层级由两次 Leiden 运行得到：**L0（Global，粗）分辨率 1.0**、**L1（Micro，细）分辨率 2.0**。
- Leiden 是随机算法，因此固定了 `seed`（`VECTOR_LAKE_LEIDEN_SEED`，默认 42）以保证可复现。
- `centrality_score` / `node_score` 仍由 `networkx.pagerank` 计算，保持原有排序语义不变。
- 社区 ID 仍由节点重叠映射到稳定 UUID，重跑不会孤儿化已有的 `System_Community_*` 索引页。

阈值参考（合成语料：3 个强内聚簇 × 8 节点）：L1 恢复出 3 个社区、簇内同社区率 100%、固定种子下可复现、社区 ID 跨重跑稳定。

#### 检索重排：bm25s

`bm25s` 提供弹性的 BM25 实现，用于 **Phase-2 同池重排**（`tool_search._rerank_candidates_locally`）：

- **候选集成员不变**（召回由上游 FTS5 + 图扩展决定），只改变池内顺序。
- 词汇信号来自 `title + summary + aliases`（不读正文：否则每次查询都要逐候选读文件）。
- 分数为**池内归一化**（min-max），不是绝对相关度：因此首位候选通常显示 `1.000`，并列最大的候选保持并列。
- 默认权重 0.4 保留上游影响力，避免把**本就无词汇重叠的图扩展候选项**压到底部。

#### CJK 分词

CJK 分词采用两层后端（统一入口 `vector_lake/tokenizer.py`）：

| 后端 | 角色 | 安装 |
|---|---|---|
| **`rjieba`（首选）** | `jieba-rs` 的官方 PyO3 绑定（同作者 messense），Rust 实现 | 必需依赖；提供 `cp38-abi3` wheel（Windows / macOS / manylinux / musllinux），**无需编译器** |
| **`jieba`（回退）** | 纯 Python，任何平台可装；也是唯一提供 `add_word()` 的后端 | 必需依赖 |

**版本真相（重要）**：`rjieba 0.2.1` 在它的 `Cargo.toml` 里钉定的是 **`jieba-rs = "0.9.0"`**，即实际生效的 Rust crate 是 **0.9.x**，而**不是** 0.11。`jieba-rs 0.11.0` 于 2026-09-16 发布，**目前没有任何已发布的 Python 绑定**；本机也无 Rust 工具链（无 `cargo`/`rustc`/`maturin`），无法从 sdist 自建。该事实在代码中以常量 `tokenizer.JIEBA_RS_PINNED` 记录，并由 `doctor` 与 `backend_version()` 直接显示，例如：

```text
[OK] Tokenizer Backend: rjieba 0.2.1 (jieba-rs 0.9.x); no add_word() on this backend
```

待 `rjieba` 发布基于 0.11 的版本后，只需同步 `requirements.txt` / `requirements.lock.txt` 的版本号与 `JIEBA_RS_PINNED`。

**已知能力缺口**：`rjieba` 不暴露 `add_word()` / `load_userdict()`（模块级与 `Jieba` 类均无），且 jieba-rs 内嵌自己的词典。因此 `tool_search.QUERY_EXPANSION_DICT` 的术语注册在 Rust 后端下**不生效**，代码会输出一次性 WARNING 而非假装成功。影响有限：索引与查询使用**同一**分词器，两侧切分一致，检索仍可命中，仅这几个术语的精确短语形态不同。绝不把词注册到 `jieba` 再指望 `rjieba` 生效（两者词典不共享）。

**实测收益与语义差异**（同一本机、3210 字符正文）：

| 指标 | 纯 Python `jieba` | `rjieba` |
|---|---|---|
| 单页分词 | 4.39 ms | **0.39 ms（11.3×）** |
| 200 字符短文本 | 0.33 ms | 0.02 ms（15.0×） |
| 9630 字符长文本 | 13.51 ms | 1.82 ms（7.4×） |
| 词元一致性（66 段项目文档中文语料） | — | 62/66 段逐词完全一致，全局词表 Jaccard **0.9957** |

差异集中在拉丁/数字边界（如 `utf-8` vs `utf`+`-`+`8`、`2018-12` vs `2018`+`12`），中文词本身几乎完全一致。

**切换后端必须重建索引**：搜索索引的内容哈希把后端身份包含在内（`indexer._node_content_digest`），因此换后端后下一次 `projection-rebuild-index --apply` 会重新分词，不会在同一 FTS 索引里混用两套分词。可用 `VECTOR_LAKE_TOKENIZER=jieba` 强制回退。

全量重建的剩余瓶颈已不在分词：warm 重建的约 50% 耗时是 `index.json` / `claim_graph.json` 的 `json.dump` 序列化。

## Module Map

`vector_lake/` 内是运行时本体：它自行调用嵌入模型，但**不发起任何非嵌入的模型调用**（任务包的 `cost_boundary`）；文本生成一律交给宿主，因此摄取 Runner 放在 `scripts/` 而非包内。

入口与核心管线：

| Path | Role |
|---|---|
| `cli.py` | 根目录薄入口，转发到 `vector_lake.cli_app` |
| `watchdog_sync.py` | 常驻守护进程入口（`watchdog_app.start_watchdog`） |
| `vector_lake/cli_app.py` | CLI 参数解析、命令路由与突变批量提交 |
| `vector_lake/mcp_server.py` | MCP 工具后端（FastMCP） |
| `vector_lake/tools.py` | Tool facade，汇聚所有 `tool_*` 模块 |
| `vector_lake/watchdog_app.py` | 文件监听、outbox 消费、增量索引、定时 lint 与 WAL checkpoint |
| `vector_lake/watchdog_status.py` | Watchdog 状态遥测（`.watchdog_status.json`，按组件聚合） |

存储与一致性：

| Path | Role |
|---|---|
| `vector_lake/db_store.py` | SQLite 连接与 PRAGMA、schema 初始化、事务、jobs 与 `mutation_outbox` |
| `vector_lake/governance_store.py` | canonical store、change set、别名注册、operational memory 与冲突解析 |
| `vector_lake/mutation_coordinator.py` | 统一突变编排：canonical 事务 + 持久化 outbox + 投影 materialize |
| `vector_lake/runtime_health.py` | 运行时健康评估与写入门（硬故障阻断 / 可修复降级放行） |
| `vector_lake/wiki_utils.py` | 路径解析、frontmatter、原子写入与位置辅助（runtime/outbox 信号目录等） |
| `vector_lake/node_vocabulary.py` | 节点类型词表的唯一来源（类型 ⇄ 前缀、严格文件名模式），零 import 的叶片模块 |
| `vector_lake/schema_validator.py` | frontmatter 与正文结构的 schema 校验 |
| `vector_lake/defense_hook.py` | 写入前防御钩子（schema + purpose 契约统一入口） |
| `vector_lake/purpose_contract.py` | 战略目的解析、摄取门、SIR 复审与 Synthesis-Proposal 阈值 |
| `vector_lake/yaml_utils.py` | YAML 存取封装 |
| `vector_lake/backup_retention.py` | `.meta/backups` 的扫描、边界解析与剪枝（最新一份永不删除） |

索引、检索与记忆：

| Path | Role |
|---|---|
| `vector_lake/indexer.py` | `index.json` / `claim_graph.json` 生成、FTS 投影、稀疏图遍历与增量更新 |
| `vector_lake/embedding_scheduler.py` | RPM/TPM 限额下的可断点向量回填（`vec_embeddings`） |
| `vector_lake/tokenizer.py` | 可插拔 CJK 分词后端（`rjieba` → `jieba`），含 `JIEBA_RS_PINNED` |
| `vector_lake/tool_search.py` | 混合检索（本地扩展 + FTS5 BM25 + 多跳 PPR + bm25s 同池重排）与 Memory Packet、上下文组装 |
| `vector_lake/claim_extractor.py` | Markdown 页面 → entity / claim / evidence / source |
| `vector_lake/tool_memory.py` | 运行态记忆的物理写回（Wiki-as-Database） |
| `vector_lake/governance_metrics.py` | 治理债务指标与合并候选枚举 |

摄取与治理工具（均在 `tools.py` 注册）：

| Path | Role |
|---|---|
| `vector_lake/tool_ingest.py` | raw 扫描、摄取任务包生成、任务领取与 `finalize_ingest` |
| `vector_lake/ingest_worker.py` | queued 作业 → subagent 任务包分发 |
| `vector_lake/native_llm.py` | 宿主 subagent 任务包协议（不自行调用文本模型） |
| `vector_lake/tool_query.py` / `tool_research.py` / `tool_purpose.py` | 查询合成、主动研究下发、战略目的复审 |
| `vector_lake/tool_review.py` / `tool_merge.py` / `semantic_merge.py` / `tool_piea.py` / `tool_bulk_reconciliation.py` | 队列评审、合并建议与合并执行、跨类型查重、批量对账 |
| `vector_lake/tool_lint.py` / `tool_gc.py` / `tool_delete.py` / `tool_rename.py` / `tool_debt.py` / `tool_doctor.py` | 自愈审计、孤儿 GC、级联删除、重命名、债务与体检 |
| `vector_lake/tool_projection.py` / `tool_timeline.py` / `tool_trace.py` / `tool_graph.py` | 投影对账与重建、时间线重建与检索、溯源、图谱可视化 |
| `vector_lake/tool_maintenance.py` | 备份保留与幂等索引维护面（`backup-retention` / `idempotency-status` / `repair-idempotency`） |
| `vector_lake/provenance.py` / `skeleton_parser.py` | 溯源追踪、结构骨架解析 |

仓库资产：

| Path | Role |
|---|---|
| `schema.md` / `SCHEMA_CATEGORIES.md` | Wiki 与运行态记忆契约、受控分类表 |
| `skills/` | 面向宿主的技能定义（每个能力一份 `SKILL.md`） |
| `templates/` | 摄取 / 查询提示词模板与拓扑可视化 HTML |
| `scripts/` | 独立维护脚本（社区聚类、语义去重、域总览、janitor 分片、purpose 校验），以及宿主侧摄取 Runner：`ingest_runner.py` + 常驻监督器 `ingest_runner_service.py` + 模型接缝 `ingest_model_pi_subagents.py` |
| `tests/` | pytest 回归套件 |

## Validation

```powershell
$env:PYTHONUTF8='1'; python -m pytest -p no:cacheprovider -q      # 与 CI 一致
$env:PYTHONUTF8='1'; python -m compileall -q vector_lake tests
$env:PYTHONUTF8='1'; python cli.py doctor
$env:PYTHONUTF8='1'; python cli.py search "<keyword>" --mode memory --top_k 3
$env:PYTHONUTF8='1'; python cli.py debt --top 1
```

本轮（2026-09-17）实测结果：

- `python -m pytest -p no:cacheprovider -q` → **689 passed**（在仓库工作副本中运行，另在排除 `.git` / `config.json` / `brain` / `tmp` 的副本上复现，结果一致）。
- `python -m compileall -q vector_lake tests` → OK。
- `python cli.py doctor` → `State Consistency: Wiki:7125 JSON:7125 SQLite:7125`（missing/extra 均为 0），`Write Gate: clean`，`Idempotency Index: jobs=full(dups=0), mutation_outbox=full(dups=0)`，`Backups: 3 entries, 10.70 GiB`（keep≤3 / ≤12.0 GiB）；`Summary: healthy with degradation`（唯一降级项是设计内的 subagent 文本运行时委托）。
- 端到端：raw 源 → `sync` → `ingest-tasks` → `finalize_ingest` → outbox 消费 → 索引 → `search` / `query` 全链路在隔离根上跑通。

**本文件不记录语料规模类数字**（节点数、边数、memory 条数）。这类数值取决于运行实例，无法从仓库复现，容易在版本迭代后变成误导性基线；需要时以目标实例上的 `doctor` / `debt` / `projection-report` 实测输出为准。

## Notes

- Windows 控制台建议设置 `PYTHONUTF8=1`，避免中文路径或中文输出触发编码问题。
- 长任务由 `filelock` 串行化（`index.json.lock`、`.meta/governance_queue.lock`、`.watchdog.instance.lock`、`<meta>/runtime/ingest_processing.json.lock`）。遇到占用时先确认没有残留的 watchdog / MCP 进程，再重试，不要直接删锁文件。
- `.gitignore` 默认忽略 `brain/`（subagent 任务包）、`tmp/`、`data/`、`*.bak`、`*.tmp`、`__pycache__/`、`.pytest_cache/`。
