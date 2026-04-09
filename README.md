# Vector Lake (LLM-Wiki 引擎 - Agentic Computable-Graph V7.1)

**Vector Lake** (V7.1) 是 Gemini CLI 的有状态、可产生复利的知识图谱与语义编译引擎。

它实现了由 Andrej Karpathy 提出的 [LLM-Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，彻底摒弃了传统的"无状态检索增强生成 (RAG)"和黑盒数据库。取而代之的是，它依赖 Gemini CLI Native Agent 来主动编译、相互链接并维护一个纯 Markdown 文件组成的持久化网络。

## V7.1 新特性（借鉴 nashsu/llm_wiki）

- **两步 Chain-of-Thought 摄取**：分析与生成物理分离。Step 1 LLM 先输出结构化分析（实体、概念、矛盾），Step 2 再基于分析驱动文件生成
- **4-信号相关性模型**：直接链接(3.0) + 源重叠(4.0) + Adamic-Adar 共同邻居(1.5) + 类型亲和(1.0)，`index.json` 新增 `weighted_edges` 字段
- **`purpose.md` 语义锚点**：定义知识库的"为什么"——核心定位、关键问题、研究范围，注入每次 LLM 操作
- **Review 异步审查队列**：LLM 在摄取时自动标记矛盾/空白/建议，用户通过 `cli.py review` 异步处理
- **`overview.md` 全景摘要**：每次摄取后自动更新的人类可读鸟瞰图
- **Token 预算控制**：60% wiki 页面 / 20% 对话历史 / 5% 索引 / 15% 系统 prompt，防止上下文溢出
- **CJK bigram 搜索 + 图扩展**：三级评分 title(+10) → summary(+3) → body(+1)，top 结果的高权重邻居自动注入候选
- **Deep Research 自动回摄**：`query` 命令执行后自动检测新文件、重建索引、为断链生成 seed 页面
- **级联删除**：`delete` 命令 3-match 策略精准清理源文件关联的所有 wiki 页面
- **10-point 结构化 Lint**：前端完整性、命名合规、ID 去重、断链检测、孤岛页、相似文件名、知识衰变

## 物理架构 (Architecture)

系统在五个截然不同的物理层面上运行，**零外部数据库依赖**：

1. **不可变信源区 (`MEMORY/raw/`)**：原始数据（PDF、文章、日志）。Agent 从中读取，**绝不修改**。
2. **活跃知识库 (`MEMORY/wiki/`)**：持久化逻辑资产。Agent 拥有绝对写权限。实体、概念、推演页面通过 `[Relation:: [[双向链接]]]` 交织。
3. **轻量级索引 (`wiki/index.json`)**：Pure-JSON 元数据索引，含 `nodes`（title/type/aliases/links/sources/summary）、`weighted_edges`（4-信号相关性权重）、`aliases` 反向映射、`categories` 词表。由 `indexer.py` 在 <0.1s 内全量重建。
4. **语义锚点 (`MEMORY/purpose.md`)**：Wiki 的战略目标与关键问题定义，注入每次 LLM 操作以确保方向一致。
5. **审查队列 (`wiki/.meta/review_queue.json`)**：异步人机协作队列，存储摄取过程中发现的矛盾、空白和建议。

## Agentic 编译流

V7.1 采用**两步 CoT + 子代理隔离 + 纵深防御**架构：

* **两步 Chain-of-Thought**：Step 1 注入 purpose + index summary → LLM 输出结构化分析；Step 2 注入分析结果 + schema → LLM 执行文件写入。物理分离确保"先想清楚再动手"。
* **Subagent 隔离**：`vector-lake-ingestor` / `vector-lake-synthesizer` / `vector-lake-collider` 三个专用 Subagent，被剥夺终端执行权限，强制锁定在文件 I/O。
* **4-信号相关性引擎**：每次索引重建时计算节点间的加权边 (direct link 3.0, source overlap 4.0, Adamic-Adar 1.5, type affinity 1.0)，驱动搜索图扩展和拓扑可视化。
* **Review 闭环**：Agent 在 Step 2 结束时输出 `---REVIEW: type | title---` 块，由 `review.py` 解析存入 JSON 队列，用户异步裁决。
* **强制中文架构**：子代理生成任何报告、实体解析和推演洞察时，强制使用高质量中文输出。
* **Prompt 注入防御**：文件路径注入前经 `_sanitize_for_prompt()` 处理，剥离控制字符、反引号和 `@mention` 劫持向量。
* **写入快照保护**：UPDATE 操作前自动调用 `_backup_wiki_targets()` 创建 `.bak` 快照。
* **增量去重**：基于 MD5 内容哈希的 `processed_files.json` 状态追踪。

## 核心模块 (Module Map)

| 模块 | 职责 |
|---|---|
| `cli.py` | Argparse CLI 路由入口（sync/search/lint/query/serendipity/graph/review/delete） |
| `tools.py` | 8 个 Tool 函数 + CJK 分词器 + Token 预算控制 + 10-point Lint + 级联删除 |
| `ingest.py` | Raw→Wiki 两步 CoT 编译管线（分析→生成 + purpose 注入 + review 解析） |
| `indexer.py` | `index.json` 全量/O(1)增量生成器（含 4-信号相关性计算 + weighted_edges） |
| `review.py` | 异步审查队列（REVIEW 块解析、JSON 持久化、CLI 展示/解决） |
| `db.py` | `processed_files.json` 线程安全读写（FileLock） |
| `watchdog_sync.py` | 文件系统实时监听哨兵 |
| `templates/topology.html` | 3D 力导向拓扑可视化 HTML 模板 |

## 核心元文件 (Core Files)

*   **`wiki/index.json`**：极速检索缓存，含 nodes（title/type/aliases/links/sources/summary）、weighted_edges（4-信号权重边）、aliases 反向映射、categories 词表。后台监听器做 O(1) 的局部更新。
*   **`wiki/overview.md`**：每次 ingest 后自动更新的全景摘要，人类可读的"鸟瞰图"。
*   **`wiki/log.md`**：按时间顺序追加的操作流水日志。
*   **`wiki/.meta/review_queue.json`**：异步审查队列持久化存储。
*   **`MEMORY/purpose.md`**：Wiki 的语义锚点——核心定位、关键问题、研究范围。
*   **`schema.md` / `SCHEMA_CATEGORIES.md`**：系统治理协议。YAML V7.1 元数据（含 domain/topic_cluster/status 必填字段）、Dataview 双向链接范式、受控类目词表。
*   **`data/processed_files.json`**：MD5 哈希状态追踪，驱动增量编译。

## 工作流与命令 (Workflows & Commands)

通过 `gemini` CLI 命令或 `python cli.py <command>` 与 Vector Lake 交互。

### 1. 自动编译管线 (Ingestion Compiler — 2-Step CoT)
当您将新文件放入 `MEMORY/raw/` 目录时，系统唤醒 Agent 执行两步 CoT：先结构化分析（实体、概念、矛盾），再基于分析驱动文件生成、overview 更新和 review 标注。

*   **手动全量编译**: `gemini sync_vector_lake` 或 `python cli.py sync`
*   **启动后台监听哨兵**: `gemini watchdog_sync` 或 `python watchdog_sync.py`

### 2. 知识检索 (Search — CJK + Graph Expansion)
CJK bigram 分词 + 三级评分（title +10 / summary +3 / body +1）+ 图扩展（top 3 结果的高权重邻居自动注入候选）。

*   **搜索**: `gemini search_vector_lake "DRG 医保"` 或 `python cli.py search "DRG 医保" --top_k 5`

### 3. 深度推演落盘 (Query-to-Page — 带预算控制 + 自动回摄)
预算控制的上下文组装（60/20/5/15 分配），推演完成后自动检测新文件、重建索引、为断链生成 seed 页面。

*   **推演查询**: `gemini query_logic_lake "对比 Vendor A 和 Vendor B 的 AI 战略差异"` 或 `python cli.py query "..."`

### 4. 结构化审计自愈 (10-Point Structural Lint)
10 项深度健康体检：前端完整性、命名合规、Type/Status 合法性、Category 词表、重复 ID、Alias 冲突、断链、孤岛页、相似文件名、知识衰变（>60 天 sprouting → 自动升级为 evergreen）。

*   **审计**: `gemini run_wiki_lint` 或 `python cli.py lint [--auto-fix]`

### 5. 审查队列 (Async Review Queue)
查看和处理 LLM 在摄取过程中标记的矛盾、重复、知识空白和建议。

*   **查看队列**: `python cli.py review`
*   **解决条目**: `python cli.py review resolve <index> [--resolution skip|create]`

### 6. 级联删除 (Cascade Delete)
删除一个原始源文件及其关联的所有 wiki 页面。3-match 策略：Source 页名匹配 + frontmatter sources 匹配，单源页 DELETE / 多源页仅移除引用。

*   **预览**: `python cli.py delete "/path/to/raw/file.pdf" --dry-run`
*   **执行**: `python cli.py delete "/path/to/raw/file.pdf"`

### 7. 随机碰撞引擎 (Serendipity Collision)
随机选取 2 个不相关节点进行正交碰撞，激发跨领域洞察。

*   **碰撞**: `gemini serendipity_lake` 或 `python cli.py serendipity`

### 8. 3D 拓扑图谱 (Graph View — 带相关性权重)
解析 Markdown 节点网络，利用 `weighted_edges` 渲染带边权的 3D 力导向关系图大屏。底部集成 Source / Entity / Concept / Synthesis 维度的全局资产仪表盘。

*   **大屏展示**: `gemini show_graph` 或 `python cli.py graph`

## 为什么采用这种架构？

传统的 RAG 系统在每次被提问时都在从零开始重新拼凑知识碎片。而 LLM-Wiki 模式将知识编译一次并保持常新。交叉引用在物理层面上真实存在；事实冲突被显式标注；逻辑综合反映了您阅读过的一切。

V7.1 进一步引入两步 CoT 摄取确保"先想清楚再动手"、4-信号相关性模型让图谱边权反映真实关联强度、purpose.md 让整个系统始终对齐战略意图、review 队列实现了人机协作的异步闭环。

维护一个维基百科所带来的庞大"脑力体力活"——现在已完全外包给了永不疲倦的 LLM。全量索引重建在 <0.1s 内完成，无需外部向量数据库，确保系统自主可控。