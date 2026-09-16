# Unreleased

## MCP 服务端依赖换为 `fastmcp>=4.0.0`

`vector_lake/mcp_server.py` 直接导入 `fastmcp.FastMCP`，移除 `mcp.server.MCPServer` / `mcp.server.fastmcp` 双路径回退。

- 同步工具清单兜底改写 fastmcp 4 的 `local_provider._components`：`_tool_manager` 在 4.x 已不存在，旧兜底会静默返回空列表；`list_tools()` 仍只有协程形态，原有“无事件循环时 `asyncio.run`”的路径不变。
- `requirements.txt`：`mcp>=2.1.0` → `fastmcp>=4.0.0`；`requirements.lock.txt` 改钉 `fastmcp==4.0.4`。MCP Python SDK（`mcp` 2.2.0）由 `fastmcp-slim[client,server]` 传递解析，不再作为直接依赖声明。
- 传递依赖净增 27 个发行包（fastmcp-slim、cyclopts、griffelib、joserfc、authlib、keyring、py-key-value-aio 等）。解析后逐版本查 OSV 均为 0 记录；fastmcp 许可 Apache-2.0，仓库 PrefectHQ/fastmcp。
- 启动期外部调用归零：fastmcp 默认会在启动时请求 pypi.org 查版本并打印 banner，现固定 `FASTMCP_CHECK_FOR_UPDATES=off` 且 `mcp.run(show_banner=False)`——stdio 通道的 stdout 承载 JSON-RPC，不能有横幅噪声。
- 验证：`registered_tool_names()` 仍返回 36 个工具；`python -m vector_lake.mcp_server` 经 stdio 实测握手列出 36 个工具并成功执行 `get_governance_debt`；全量 pytest 通过。

# Vector Lake 11.18.0

## 核心流程审计修复：两条主流程断路、两处静默数据丢失、隐私排除失效

完整审计报告见 `AUDIT_2026-09-16.md`。共 17 项缺陷，本次修复 P0（5）与 P1（5），P2 做机械性清理。
**根因是同一类 Python 语义缺陷 + 同一类守护网盲区**，因此除了修缺陷本身，也修了让缺陷通过的守护规则。

### 1. `prepare_ingest_batch` 必然 `UnboundLocalError` —— 入库流水线长期整体失效

函数体末尾的 `import json` 使 `json` 成为该函数**局部名**，其前的 `json.load()` 必然未绑定。两条并行后果：

- 配置加载被 `except Exception: config = {}` 吞掉 → **`exclude_paths`（`stocks/` / `garmin/` / `personal-insights/`）被静默忽略，隐私排除目录照样进库**；
- `json.dump()` 在 `except` 之外 → 只要存在待入库文件就抛 `UnboundLocalError`。

修复：删除函数内 import；配置加载改为 `_load_scan_config()`（文件缺失=空配置，**不可读或非法 JSON 直接报错**，不再静默降级）；补 `init_db()`（全新知识库曾抛 `no such table: processed_files`）。

顺带修复**冷启动断路**：`wiki/index.json` 不存在时 `_read_relevant_index_context` 直接抛错，而索引正是入库之后才产生的 → 空知识库永远无法完成首次入库。现区分两种情况：**无 wiki 页面 = 合法空上下文**（冷启动），**有页面但无索引 = 真实投影故障，仍报错**（否则会批量产生重复实体）。

### 2. `create_change_set` 引用未定义名 —— canonical 引导路径全灭

`existing_change_sets["items"].append(...)` 中的名字从未定义 → 每次调用 `NameError`，且发生在事务内 → 回滚。传播链：`ensure_canonical_store_populated()` → `migrate_existing_wiki(dry_run=False)` → `create_change_set()`，被 `graph`、`trace`、`governance_projection` 使用。**恰好在最需要它的场景（wiki 有页面、canonical 为空）不可用。**

同时修掉三个同类问题：

- `create_change_set(dry_run=True)` 此前**完全被忽略**，调用方拿到的是一次真实生效并落盘的变更集 → 现返回预览，不落盘（实测：dry-run 后 `change_sets` / `entities` 均为 0）；
- `sync_pages_to_canonical` 用 `os.path.exists(path)` 判断“页面已删除”：传入**裸文件名**时永远为假 → **删掉 canonical 实体而 Markdown 仍在盘上**。现先按 wiki 目录解析相对路径；
- `create_change_set` 中 4 个 `load_entities()/load_claims()/load_evidence()/load_sources()` 全表载入已无使用（SQLite 重构残留）→ 删除（每次调用省 4 次全表扫描）。

### 3. 治理队列丢失更新：实测 4 线程 × 8 条丢失 24 条

`_save_db_queue` 以“键差集替换”落盘，会 `DELETE` 调用方快照中不存在的所有键。任何未持锁的 `load → append → save` 都会删掉并发写入者在此窗口追加的条目。此前**部分**写入者取了 `governance_queue.lock`、部分没有，锁形同虚设。

实测（修复前，4 线程 × 8 条）：

```
UNLOCKED writers: expected 32, persisted 8, lost 24
```

修复：新增 `governance_queue_session()` —— 单一缓存 `FileLock` 实例 + 线程内可重入，**整个 load → mutate → save 周期持锁**；全部写入者改道：`enqueue_governance_item(s)`、`create_merge_suggestions`、`create_change_set`、`create_change_set_from_content`、`publish_change_sets`、`governance_service.resolve_governance_item`、`mcp_server.propose_schema_mutation`、`tool_graph.audit`、`tool_bulk_reconciliation`、`scripts/community_clustering_daemon.py`、`scripts/semantic_dedup_daemon.py`。

锁序统一为 **文件锁 → 数据库事务**，消除 `create_change_set`（事务内取文件锁）与 `propose_schema_mutation`（文件锁内取事务）之间的死锁条件。耗时较长的语义去重扫描不持锁，改为**在锁内重新读取最新快照后再合并**。

### 4. `claim_graph_edges` 删除条件从未匹配 → 永久悬挂边

原代码 `DELETE FROM claim_graph_edges WHERE source_id IN (page_keys)`：该表的键空间是 **claim_id**，用 page_key 过滤**永远匹配 0 行**；即便修正也只删 `source_id` 一侧。现按本次增量**实际触及的 claim_id**（旧 + 新）双端删除，再由 `save_graph_edges` 回填存活边。

同时解除 `page_graph_edges` 的双写：该表是 indexer 的纯投影（`replace_page_graph_edges[_for_node]` 已双向替换），治理侧不再写它。

### 5. `delete_source` 级联删除：前缀误匹配、无恢复点、`processed_files` 泄漏

- `startswith("source_annual")` 会连 `Source_AnnualReport.md` 一起删 → 改为与 canonical 源页名**精确相等**；
- `raw_basename in source` 子串匹配会命中 `raw/archive/AnnualReport.md` → 改为按 `normalize_raw_ref` 归一化后**精确比较**（或文件名相等）；
- 删除 Markdown 前**先建恢复点** `backup/delete-source/<stamp>/`（对照 `tool_gc` 已有备份机制）；备份失败则整体中止、不做任何改动；
- 删除原始源后清理 `processed_files` 行（此前同源重新加入会因 hash 相同被判定“已处理”，永不入库）。

实测：删除 `raw/Annual.md` 时 `Source_AnnualReport.md` 保留、`Concept_Unrelated.md` 保留、`Source_Annual.md` 删除且已备份、`Concept_Multi.md` 移除引用、`processed_files` 1 → 0。

### 6. 不可观测失败的修复

| 缺陷 | 原状 | 现状 |
|---|---|---|
| `finalize_ingest` 异常吞噬 | 数据库/outbox/健康门故障与“载荷校验不通过”返回**同一种字符串**，工具调用上报成功 | 仅**调用方可修复的拒绝**（`ValueError` / `SafeWriteError` / `PurposeContractError` / `SchemaViolationException` / `DefenseHookException`）保留消息契约；**基础设施故障带堆栈抛出** |
| outbox 唤醒信号 | 生产写 `<ext>/tmp/`、消费读 `%TEMP%/vector_lake_tmp/` → **永不一致的死信号** | 双方统一到 `<meta>/runtime/outbox_signal.lock`，实测信号被消费 |
| 入库在途状态 | 放在系统临时目录（**跨知识库共享**）、以**内容 hash** 为键（同内容不同文件被静默丢弃）、**失败不释放**（源被阻塞 1 小时 TTL） | 移入 MEMORY 根 `<meta>/runtime/`、以**解析后的 filepath** 为键、原子写入、**入队失败即释放**（实测失败后重试可再次入队） |
| 日记同步 | `Popen(stdout=DEVNULL, stderr=DEVNULL)`，失败**完全不可见** | 工作线程 `subprocess.run` + 超时；退出码/stderr 写入 `write_status(component="diary")`，成功时清回 `idle`（实测 exit=3 被记录） |
| `assemble_context` | 用正则反向解析自己的格式化输出；`purpose` **无长度上限**，`budget_used` 可超 `budget_max` | 抽出 `_search_scored_pages()` 返回结构化结果，上下文直接消费；预算不变量 `budget_used <= budget_max` 在 200 与 20000 字符下均成立 |

### 7. 回归守护网本身修复（让 F1 通过的那条规则）

`tests/test_static_scope.py` 的 `_bound_in_scope` 把函数内**任意位置**的 import 记为**全函数可见**，因此**结构上看不见“绑定前引用”**——这正是 F1 的形态。新增 `use_before_local_import()`：对每个函数内 import 取**源码顺序中最早的绑定行**，任何更早的读取即判定为缺陷（嵌套函数/类/lambda 内的读取不计，因为它们可能晚于 import 执行）。对照实测：对修复前的 `tool_ingest.py` 报 3 处，修复后 0 处。

### 8. 其它

- 连接 PRAGMA 移入 `get_connection()`：`init_db` 按 db 路径记忆化，导致 `close_connection()` 之后的新连接（outbox 消费线程每轮回收连接）**静默失去 `foreign_keys` / `synchronous`**。当前全库无外键声明，属潜伏缺陷；实测已修（回收后 `fk=1 sync=1 journal=wal`）。
- 删除 `wiki_utils` 中**重复定义**的 `SafeWriteError`，以及 `write_markdown_file` 里被自身 `except Exception: pass` 捕获、从未执行的死边界检查（真正生效的是末尾 `expected_path` 精确比较）。
- `native_llm._task_root` 每进程创建 `brain/runtime-<pid>-<uuid>/` 且**无回收**（实测累积 50+ 个）：新增保守清理——**仅删除超过 7 天且不含任何文件**的 `runtime-*` 目录。
- 仓库级 `ruff` 清理：未用 import / 重复定义 / 未用局部变量；`except Exception as e: pass` 改为带上下文的告警。

### 9. 新增回归测试（+88 项）

- `tests/test_static_scope.py`（+59）：绑定前引用检测，含“守护必须能在修复前的形态上失败”的自检。
- `tests/test_governance_queue_concurrency.py`（4）：**确定性交错**证明未持锁保存会删掉对端条目；持锁并发 32 条零丢失；可重入。
- `tests/test_canonical_change_sets.py`（7）：`create_change_set` 不再 `NameError`、`dry_run` 零落盘、引导只执行一次、裸文件名不作删除、真实删除仍生效、派生边双端删除。
- `tests/test_runtime_coordination.py`（10）：信号路径一致、入库在途状态作用域与失败释放、同内容双源各自入队、级联删除精确匹配/恢复点/`processed_files` 清理、过期 runtime 目录清理。
- `tests/test_context_assembly.py`（8）：上下文不再依赖格式化输出、预算不变量、检索降级可见、缺失索引上报。

**回归基线：426 项测试全绿**（修复前 338 项）。`ruff check vector_lake scripts tests *.py --select F401,F811,F841,F821,F823,F541,E721` 全清。

# Vector Lake 11.17.0

## 依赖与算法变更：Leiden 取代 Louvain，新增 bm25s，抬高版本下限

### 1. `python-louvain` → `igraph>=0.11.0` + `leidenalg>=0.10.0`

- 社区检测改为 **Leiden**。Louvain 的 dendrogram 层级被 `resolution_parameter` 取代，因此两个层级由两次运行得出：L0(Global)=1.0、L1(Micro)=2.0（可用 `VECTOR_LAKE_LEIDEN_L0/L1_RESOLUTION` 调整）。
- Leiden 是随机算法 → 新增固定种子 `VECTOR_LAKE_LEIDEN_SEED`（默认 42），否则每次聚类结果都不同，社区页会反复孤儿化。
- `nx.pagerank` 仍负责 `centrality_score`/`node_score`，**排序语义未变**；`networkx` 因此保留。
- 实际消费者只有 `scripts/community_clustering_daemon.py`；`vector_lake/indexer.py` 里早已失效的 `networkx`/`community` 导入属于死代码，已删除。
- 两者均提供 `cp39/cp38-abi3` wheel，无需编译器。

**顺带修复的既有缺陷**：`System_Community_*` 页面的 frontmatter 缺少 `id` / `categories` / `updated`，导致 `execute_mutation_batch` 必定抛 `DefenseHookException`——即聚类守护进程自 V11.10 统一突变协调器之后从未真正成功过（图永远保持 dirty）。已补齐模板并通过 `validate_schema` 实测。

### 2. 新增 `bm25s>=0.2.0`：Phase-2 同池重排

`tool_search._rerank_candidates_locally` 此前是空桩（永不重排）。现已用 bm25s 实现：

- **候选集成员不变**，只改变池内顺序 → 召回不受影响。
- 分词经项目自身的 `tokenizer`（bm25s 默认 `\w\w+` 无法切中文）。
- 分数为池内 min-max 归一化（**非绝对相关度**）；`VECTOR_LAKE_RERANK_WEIGHT=0` 可完全恢复旧排序，异常时 fail-open 保持原顺序。
- 保留 40% 上游权重是刻意设计：图扩展候选本就无词汇重叠，否则会被压到底部。

**行为变化提醒**：检索结果的 `score` 显示由无界 BM25 量级变为 `[0,1]` 池内归一化值，格式化精度由 `.1f` 改为 `.3f`。

### 3. 版本下限抬高（均已实际安装与验证）

| 依赖 | 新下限 | 核验方式 |
|---|---|---|
| `filelock` | `>=3.15` | 已装 3.29.0，使用的 API 长期稳定 |
| `networkx` | `>=3.2` | 已装 3.6.1 |
| `PyYAML` | `>=6.0.1` | 已装 6.0.3 |
| `google-genai` | `>=2.0.0` | **下载 2.0.0 wheel 核实**：`UserContent`/`Part.from_text`/`HttpOptions`/`EmbedContentConfig.output_dimensionality` 均存在 |
| `sqlite-vec` | `>=0.1.3` | **隔离 venv 实跑 0.1.3**：`vec0(TEXT PRIMARY KEY, float[3072])` + `MATCH`+`ORDER BY`+`LIMIT` 全部通过 |
| `mistune` | **`>=3.0.1`**（偏离字面值） | **二分实测**：3.0.0 的 `create_markdown(renderer='ast')` 抛 `TypeError: 'str' object is not callable`，3.0.1 起正常。写 `>=3.0` 会是一个虚假下限 |

注：`igraph>=0.11.0` 的下限版本 0.11.0 并未发布（0.11.2 起才有 abi3 wheel），但作为约束合法，pip 会解析到最新版。

### 4. `doctor` 新增 `Clustering Backend` 行

显示实际生效的 `leidenalg`/`igraph` 版本，避免算法替换后无法确认运行时状态。

### 5. 新增回归守护

- `tests/test_clustering_leiden.py`（9 项）：群落恢复、分辨率单调性、种子可复现、空图/自环/缺权重健壮性、模板 schema 合规。
- `tests/test_rerank_bm25s.py`（13 项）：成员不变、词汇相关项上升、权重 0 可复现旧序、bm25s 缺失/抛错 fail-open、分数区间、端到端渲染。
- `tests/test_dependency_manifest.py`（23 项）：依赖下限、`python-louvain` 彻底移除（含运行时代码扫描）、`mistune>=3.0.1` 不得回退。

回归基线：**338 项测试全绿**。

# Vector Lake 11.16.0

## 分词后端换为 `rjieba`（jieba-rs / Rust）

- 新增必需依赖 **`rjieba>=0.2.1`**（jieba-rs 的官方 PyO3 绑定，作者同 messense，MIT）。它提供 `cp38-abi3` wheel，本机实测直接命中 wheel、**无需任何编译器**（与上轮被否决的 `jieba-fast` 根本不同）。
- 后端链改为 **`rjieba` → `jieba`**（后者保留为纯 Python 回退，且是唯一提供 `add_word()` 的后端）；`VECTOR_LAKE_TOKENIZER=jieba|rjieba` 可强制，强制不可用时告警并回退。
- 移除上一轮的 `jieba_fast` 后端及其可选依赖文件 `requirements-accel.txt`（已放弃）。

### 版本真相（**未达到要求的 0.11**）

- `rjieba 0.2.1` 的 `Cargo.toml` 钉定 **`jieba-rs = "0.9.0"`**，即实际生效的 crate 是 **0.9.x**，**不是 0.11**。
- `jieba-rs 0.11.0` 于 2026-09-16 发布，但**没有任何已发布的 Python 绑定**；本机无 `cargo`/`rustc`/`maturin`，无法从 sdist 自建，且自建产物无 wheel、对他人不可复现。
- 该事实以 `tokenizer.JIEBA_RS_PINNED = "0.9.x"` 硬编码记录，并由 `backend_version()` 与 `doctor` 直接输出：`rjieba 0.2.1 (jieba-rs 0.9.x)`。待绑定跟进后同步版本号即可。

### 能力缺口（已量化，不静默）

- `rjieba` **不暴露 `add_word()` / `load_userdict()`**，因此 `tool_search.QUERY_EXPANSION_DICT` 的术语注册在 Rust 后端下无效。已改为一次性 WARNING 明确报告，而非假装成功。
- 影响有限：索引与查询使用同一分词器，两侧切分一致，检索仍可命中，仅这些术语的精确短语形态不同。

### 实测收益与语义差异

| 指标 | 纯 Python `jieba` | `rjieba` |
|---|---|---|
| 3210 字符单页分词 | 4.39 ms | **0.39 ms（11.3×）** |
| 200 字符 | 0.33 ms | 0.02 ms（15.0×） |
| 9630 字符 | 13.51 ms | 1.82 ms（7.4×） |
| N=5000 冷启动全量重建 | 101.1 s | **71.6 s** |
| N=5000 warm / 单节点变更重建 | 0.70 s / 0.68 s | 0.49 s / 0.50 s |

词元一致性：66 段项目文档真实中文语料中 **62/66 段逐词完全一致**，全局词表 **Jaccard 0.9957**；差异集中在拉丁/数字边界（`utf-8` vs `utf`+`8`、`2018-12` vs `2018`+`12`），中文词几乎一致。

### 附带发现

冷重建从 101.1 s 降到 71.6 s（30%），远低于分词本身的 11× —— 说明瓶颈已转移。对 warm 重建做 `cProfile`：`json.dump` 序列化 `index.json` + `claim_graph.json` 占 **~50%**，`load_entities`/`build_claim_graph_projection`/`fetchall` 合计约 20%，分词已不再是热点。

# Vector Lake 11.15.0（已被 11.16.0 取代，保留为决策记录）

> 本节记录的是**被否决**的 `jieba-fast` 方案及其证据，不是当前状态。当前后端见 11.16.0。

## 分词后端改为可插拔（默认仍为 `jieba`）

**未将 `jieba-fast` 写入必需依赖**，原因是它在当前约束下无法安全落地（逐条为实测/核验结果）：

- PyPI 只发布 sdist（`jieba_fast-0.53`，上传于 2018-12-20），**0 个 wheel**，无任何平台预编译。
- 安装需 C++ 工具链；本机无 `cl.exe` 也无 VS 安装目录，`pip install jieba-fast` 实测失败：`error: Microsoft Visual C++ 14.0 or greater is required`。
- `setup.py` 仍为 `from distutils.core import setup`（`distutils` 已在 Python 3.12 从标准库移除）；其分类器只声明到 Python 3.7。
- 模块名是 `jieba_fast` 而非 `jieba`（包内 `__version__ = '0.39'`），与项目钉定的 `jieba 0.42.1` 分词结果不同。
- **若列为必需依赖**：无编译器的机器（绝大多数）安装即失败，而自带 MSVC 的 CI 反而通过——这是最危险的组合。

交付的形态：

- 新增 `vector_lake/tokenizer.py` 作为唯一分词入口：优先 `jieba_fast`，否则回退 `jieba`；`VECTOR_LAKE_TOKENIZER=jieba|jieba_fast` 可强制，强制后端不可用时**只告警并回退**，不会禁用分词。
- 4 处直接 `import jieba` 全部改为经该入口（另有测试禁止再次直连）。
- `requirements.txt` 改为钉定 `jieba>=0.42.1`；新增可选 `requirements-accel.txt`（仅在有编译器的机器安装）。
- `doctor` 新增 `Tokenizer Backend` 行，显示实际生效的后端与版本。
- **搜索索引的内容哈希纳入后端身份**：切换后端会触发重新分词，避免同一 FTS 索引里混用两套分词结果。

热点定位（说明加速器的收益上限）：对 3210 字符正文做 `cProfile`，`get_DAG` + `calc`（即 `jieba-fast` 用 C 替换的 `_get_DAG_and_calc`）占总耗时的 **~65%**，HMM `viterbi` 只占 ~8%；纯 Python 基线实测 **5.1 ms/页**。

# Vector Lake 11.14.0

本轮为审计驱动的缺陷修复，未引入新功能。回归基线：**156 项测试全绿**（原基线 110 项已过期，且修复前存在 1 项失败）。

## 数据安全 (P0)
- **GC 不再按文件时间删除数据**。`save_graph_edges` 曾把 claim 边原样复制进 `page_graph_edges`，而 GC 以 `page_key` 为键计算度数，导致所有页面度数恒为 0，退化为“按 mtime 删除”。现在 `page_graph_edges` 由图索引器独占写入（page-key 空间），孤儿判定改用 canonical 拓扑连通度（`links` / 共享来源 / claim 共现），并新增 50% 批量删除断路器与 `--force` 显式覆盖。
- `gc` / `delete` 的 CLI 默认改为 dry-run，需 `--apply` 才落盘，与其余维护命令一致。
- `change_sets` 清理前先轮转 JSONL 备份；备份失败则跳过删除。

## 写入可用性 (P0)
- **写健康门不再阻断自己的修复通道**。投影漂移、watchdog 心跳过期、outbox 未及时消费改判为“可修复降级”，只告警不阻断；仅硬故障（数据库不可用、outbox hard-failed 行、outbox 积压超过高水位）才阻断写入。
- **人工编辑回路恢复**。`watchdog_app.index_worker_loop` 的 legacy 分支引用了四个未导入的名字（`transaction` / `sync_pages_to_canonical` / `_utc_now` / `get_connection`），任何手工编辑 `wiki/*.md` 都会抛 `NameError` 并静默丢失。该分支已收敛为统一走 `execute_mutation_plan`，失败时保留原文并记录原因。
- `atomic_write_text` 不再吞掉非 `DefenseHookException` 的校验异常。
- `watchdog_status` 写盘失败不再静默；`existing_embedding_ids` 不再把读取失败伪装成“全部缺失”。

## 并发与一致性 (P0)
- `generate_index` 现在持有与增量更新相同的 `index.json.lock`，不再静默覆盖并发写入（旧行为会丢弃这期间所有已标记完成的 outbox 变更）。
- 全量重建不再把整段分词放进单个 `BEGIN IMMEDIATE`：改为逐节点短事务，并对内容哈希未变的节点跳过分词。5000 节点实测：冷重建 140s → 101s，无变更重建 140s → **0.70s**。
- `refresh_graph_topology_if_dirty` 在释放索引锁与事务之后才触发全量重建。

## 效率
- `index.json` 不再重复保存正文（`raw_text`）：3000 节点实测 8786 KB → **2011 KB**。
- 写健康门的目录扫描按目录时间戳缓存：3000 节点实测 287 ms → **29 ms**（常驻进程中）。
- `upsert_search_index` 不再二次分词（此前每节点 jieba 运行两次）。
- 移除死代码：`_VECTOR_CACHE`、`generate_index` 中未使用的 `load_entities`/`load_claims`、`search_vector_lake` 中未使用的全量节点拷贝。

## 检索正确性
- 图扩展与重排不再绕过调用方过滤条件（此前 `domain=` 会返回其他 domain 的页面）。
- PPR 为非种子节点恢复 teleportation 质量，不再在两次迭代后退化为“与种子相邻”。
- 向量不可用时 `search` 输出 `[DEGRADED]` 横幅，标明降级原因；不再静默返回 BM25-only 结果。
- 嵌入单条文本增加 token 上限钳制（中文约 1 token/字符，原 15k 字符上限可能超出模型窗口）。

## 中文语料
- `_normalized_name` 不再把所有中文实体归一化为空串。修复前 10 个无关中文实体产出 45/45 假合并候选，现在为 0；真实重复仍可检出；全角/半角与空白变体归并。

## 运维与可移植性
- `wiki-restore` 经协调器原子写入，并在输出中提示后续所需的索引重建。
- `config.json` 不再携带个人绝对路径；`target_directories` 留空即回落到 `<MEMORY>/raw`。
- `watchdog_app` 的 Diary / raw 监听目录改用活动 MEMORY 根；`check_jobs.py`、`reset_jobs.py`、`scripts/launch_janitor_swarm.py` 去除硬编码路径与未定义名字（后者此前必然 `NameError`）。
- 补齐 `commands/query.toml` 与 `commands/timeline.toml`；`requirements.lock.txt` 改为直接依赖的诚实钉版（原文件是含 `akshare`/`azure`/`bcrypt` 的环境快照）。
- `doctor` 不再对 `GEMINI_API_KEY` / Subagent Text Runtime 恒报 OK，并区分硬故障与降级。

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
