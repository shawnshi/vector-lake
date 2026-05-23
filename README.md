# Vector Lake

Vector Lake 是一个本地文件优先的知识编译器。它不是传统向量库，也不是一次性 RAG 后端，而是把原始材料持续编译成可审计的 Markdown wiki，并同步生成面向 Agent 的结构化运行态记忆。

当前架构边界：

- `MEMORY/raw`：原始信源层，只读输入。
- `MEMORY/wiki`：人类可读的 Markdown 发布层，用于审计、浏览、复盘和长期资产沉淀。
- `MEMORY/wiki/index.json`：页面级运行索引，用于搜索和拓扑扩展。
- `MEMORY/wiki/claim_graph.json`：claim 级逻辑图投影。
- `MEMORY/wiki/.meta/*.json`：canonical governance store，保存实体、断言、证据、信源、变更集和治理队列。
- `MEMORY/wiki/.meta/operational_memory.json`：Agent 运行态记忆层，把 `Claim` 编译为 `fact / preference / decision / task_state`。

如果 `MEMORY/wiki/.meta` 不可写，运行时会回退到仓库内 `data/v8_meta/`。

## Architecture

```mermaid
graph LR
    RAW["MEMORY/raw<br>Immutable sources"] --> INGEST["Native Subagents<br>Asynchronous Ingestion Pipeline"]
    INGEST --> WIKI["MEMORY/wiki<br>Markdown pages"]
    WIKI --> INDEX["index.json<br>page index + BM25"]
    WIKI --> META[".meta/*.json<br>canonical store"]
    META --> CLAIM["claim_graph.json<br>claim topology"]
    META --> MEMORY["operational_memory.json<br>agent runtime memory"]
    MEMORY --> PACKET["Memory Packet<br>selective context injection"]
    INDEX --> QUERY["search<br>LLM Expansion + BM25 + Graph Spreading"]
    CLAIM --> QUERY
    PACKET --> QUERY
```

核心原则：**Markdown 是人类界面，`.meta` 是事实底座，`operational_memory` 是 Agent 运行层。**

## Quick Start

1. **环境配置**：检查 `config.json`，确保 `target_directories` 路径正确，`supported_extensions` 配置了允许扫描的后缀。**必须在环境变量中设置 `GEMINI_API_KEY`**，系统已全面迁移至原生的 `google-genai` Python SDK，不再依赖 `gemini.cmd` 的子进程调用。
2. **单次编译**：执行 `python cli.py sync`，将 raw sources 编译为可读的 Markdown Wiki 并构建事实底座。
3. **后台监听与自治管理**：日常运行 `python watchdog_sync.py` 启动守护进程。它搭载了四大核心基建与防御系统：
   - **双轨看门狗 (Two-Track Watchdog)**：不仅监听增量文件生成，还实现了对 `on_deleted` 与 `on_moved` 事件的瞬间捕捉，彻底消除因 Semantic GC 产生的图谱“幽灵节点”。
   - **API 熔断器 (Circuit Breaker)**：在 LLM 并发摄入时，通过带抖动的指数退避（Exponential Backoff with Jitter）与黑名单冷却机制，彻底消除死锁、配额枯竭与 429 限流风暴。
   - **I/O 批处理防抖 (I/O Debouncing)**：将 BM25 的 O(1) 内存更新合并打包，单批次文件修改仅触发一次 `index.json` 的写盘，彻底消灭 O(N) 的磁盘 I/O 磨损。
   - **全自动自愈与战术闭环 (Autonomous Sub-Daemons)**：每天 10:00 和 23:00 执行的后台任务。包含无锁图谱排误、`metadata_decay_daemon.py` 降权超期知识、`sync_timeline_db.py` 提取时序流水账、以及 `missing_evidence_scout.py` 自动扫描缺失证据并抛入治理队列。最后以 `SQLite WAL TRUNCATE` 结束，保证存储十年不膨胀。
4. **日常搜索**：使用 `python cli.py search "<keyword>"` 或 `python cli.py query "<question>"` 检索编译后的知识网络。
5. **周期治理**：定期执行 `python cli.py review` 处理冲突队列，执行 `python cli.py doctor` 检查健康度。

## Operational Memory

运行态记忆由 `vector_lake/governance_store.py` 从 canonical claims 编译生成。它解决的问题是：Agent 常常只需要一个事实、偏好、决策或任务状态，不应该每次加载整页 Markdown。

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

## Storage Layout

```text
MEMORY/
  raw/
  wiki/
    *.md
    index.json
    claim_graph.json
    .meta/
      entities.json
      claims.json
      evidence.json
      sources.json
      operational_memory.json
      alias_registry.json
      change_sets.json
      governance_queue.json
      vector_lake.db  <-- Unified SQLite Store (Entities, Claims, Graph, Timeline)
```

## Commands

> **Note (v8.3+)**: Vector Lake 现已全面接入 MCP (Model Context Protocol)。大语言模型 Agent 将直接通过 `vector_lake/mcp_server.py` 调用底层 Tool 接口，不再需要通过终端模拟。
> 
> **Gemini CLI Slash Commands**: 我们已将常用功能映射为快捷指令（在聊天框输入 `/` 触发）：
> - `/vl_sync`：自动调度 Ingestor 子智能体执行图谱知识的异步增量同步
> - `/search`：语义搜索向量湖索引
> - `/query`：深度逻辑推理与查询
> - `/review`：检查统一治理队列
> - `/resolve`：处理治理队列中的待办项
> - `/audit`：合成图拓扑并执行审查
> - `/debt`：查看图谱治理债务指标
> - `/lint`：执行节点健康度自愈审查
> - `/research`：自主扫描并下发网络检索指令
> - `/graph`：直接生成并刷新 3D 可视化拓扑面板
> - `/doctor`：运行环境与依赖健康体检
> - `/gc`：垃圾回收与孤儿节点自动清理
> - `/delete`：级联删除信源与切断图谱边
> - `/trace`：展示实体或知识断言的溯源追踪
> - `/merge`：扫描并提出知识合并建议
> - `/timeline`：搜索与提取战略时间线事件
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
python watchdog_app.py
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

## Config

`config.json` 控制运行范围和模型调用：

- `target_directories`：raw source 扫描路径。
- `exclude_paths`：排除目录。
- `supported_extensions`：当前启用的输入扩展名。
- `processed_files_path`：已处理 raw 文件记录。
- `llm.model_cascade`：Google GenAI SDK 模型降级链（例如 `["gemini-2.5-pro", "gemini-3.1-pro-preview"]`）。
- `llm.batch_size`：批处理规模。
- `llm.timeout_analysis / timeout_generation / timeout_query`：LLM 调用超时。

## Module Map

| Path | Role |
|---|---|
| `cli.py` | 根目录薄入口 |
| `vector_lake/cli_app.py` | CLI 参数与命令路由 |
| `vector_lake/tools.py` | Tool facade |
| `vector_lake/tool_ingest.py` | Raw-source 批量扫描与 Subagent 摄取指令生成 |
| `vector_lake/indexer.py` | `index.json` 生成，含 BM25 纯 Python 倒排索引 |
| `vector_lake/claim_extractor.py` | Markdown page -> entity/claim/evidence/source |
| `vector_lake/governance_store.py` | canonical store、change set、operational memory、conflict resolver |
| `vector_lake/governance_metrics.py` | debt metrics 和治理统计 |
| `vector_lake/tool_search.py` | 混合检索管线 (LLM Query Expansion + BM25 + Graph Traversal) 与 Memory Packet |
| `vector_lake/tool_query.py` | query-to-page synthesis |
| `vector_lake/tool_research.py` | 拓扑图谱洞察分析与主动深度研究下发 |
| `vector_lake/tool_review.py` | legacy/governance review surface |
| `vector_lake/tool_doctor.py` | runtime 体检 |
| `vector_lake/mcp_server.py` | Model Context Protocol (MCP) 后端服务入口 |
| `vector_lake/watchdog_app.py` | 增量监听后台服务，队列调度，定时自愈审计 (Scheduled Auto-Lint) |
| `vector_lake/watchdog_status.py` | Watchdog 状态遥测面板 (Status JSON) |
| `vector_lake/wiki_utils.py` | Path resolution, frontmatter, atomic writes, backups |
| `schema.md` | Wiki 与运行态记忆契约 |
| `commands/` | 面向 Agent 的宏大业务流定义 (research/review) |
| `agents/` | ingestor / synthesizer agent 契约 |

## Validation

最近验证基线（2026-05-23）：

```powershell
$env:PYTHONUTF8='1'; python -m unittest discover -s tests -p 'test_*.py' -v
$env:PYTHONUTF8='1'; python -m compileall vector_lake tests
$env:PYTHONUTF8='1'; python cli.py doctor
$env:PYTHONUTF8='1'; python cli.py search "deployment target" --mode memory --top_k 3
$env:PYTHONUTF8='1'; python cli.py debt --top 1
```

验证结果：

- Unit tests：`Ran 8 tests ... OK`
- Compile：`python -m compileall vector_lake tests` OK
- Doctor：healthy
- Operational memory smoke：OK
- Debt snapshot：
  - `operational_memory_count: 13755`
  - `superseded_memory_count: 510`
  - `conflicted_memory_count: 0`
  - `memory_type_counts: {'fact': 11881, 'decision': 1393, 'task_state': 384, 'preference': 97}`

## Notes

- Windows 控制台建议设置 `PYTHONUTF8=1`，避免中文路径或中文输出触发编码问题。
- 本仓库可能存在 live file lock；如果 `index.json` 或 `.meta` 文件正在被其他进程占用，先释放锁再重建。
- `*.bak`、`*.tmp`、`tmp/`、`data/` 默认被 `.gitignore` 忽略。
