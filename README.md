# Vector Lake (LLM-Wiki 引擎 - Agentic Computable-Graph V7.2)

**Vector Lake** (V7.2) 是 Gemini CLI 的有状态、可产生复利的知识图谱与语义编译引擎。

它实现了由 Andrej Karpathy 提出的 [LLM-Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，彻底摒弃了传统的"无状态检索增强生成 (RAG)"和黑盒数据库。取而代之的是，它依赖 Gemini CLI Native Agent 来主动编译、相互链接并维护一个纯 Markdown 文件组成的持久化网络。

## V7.2 新特性 (The Topology Edition)

- **拓扑审计与图谱裂缝发现 (Louvain Integration)**：在 `indexer.py` 环节原生集成 Louvain 算法，自动划分知识社区并提取**动态主题标签**（如 `Comm 2: Agentic AI / Vector Lake`）。系统同时计算内聚度 (Cohesion Score)，侦测孤立节点、稀疏知识区与跨域桥接节点。
- **自动捕食闭环 (Autonomous Deep Research)**：引入 `audit-graph` 指令。系统自动将图谱裂缝合成为预定义研究任务注入异步审查队列。人类批准后，系统自动截获指令、触发网络搜索爬取资料并执行回摄，实现知识边界的自主扩张。
- **3D 物理视觉引擎 (ForceGraph3D)**：彻底重写 `topology.html`，从 2D 升级回 3D WebGL 球形渲染。保留并增强了高级 UI：节点度数缩放、双轨制着色切换（本体分类 / 拓扑社区）、带动态类别徽章的悬停信息卡片、以及相机自动聚焦的搜索框。
- **严酷本体论降维 (Strict Ontology Coercion)**：在索引生成阶段，强制拦截并把 LLM 创造的幻觉分类（如 Project、Person 等）降维归一化为 4 种基础类型（Entity, Concept, Source, Synthesis），保障了全局数据资产的结构纯净。
- **孤岛链接解析 (Orphan & Alias Resolution)**：图谱生成器在前端静默解析双向链接的别名映射，并剔除指向尚未建立的空气节点的断链，彻底清除了图谱 UI 中烦人的 `[UNKNOWN]` 节点。
- **双轨制契约 (Dual-Track Schema)**：实体页面物理切分为液态的“编译共识 (Compiled Truth)”与追加写入的固态“证据时间线 (Timeline)”。
- **检索降噪 (Diversity Capping)**：引入前置查询裂变 (Query Expansion) 与后置类型压制，最高限制 Source 类型占 60%，防止长尾数据淹没上下文。
- **并发安全互斥 (Global FileLock)**：在所有的 `index.json` 读写边界加装排他锁，彻底根治 Windows 下的并发 I/O 破损。

## 物理架构 (Architecture)

系统在五个截然不同的物理层面上运行，**零外部数据库依赖**：

1. **不可变信源区 (`MEMORY/raw/`)**：原始数据（PDF、文章、日志）。Agent 从中读取，**绝不修改**。
2. **活跃知识库 (`MEMORY/wiki/`)**：持久化逻辑资产。Agent 拥有绝对写权限。实体、概念、推演页面通过 `[Relation:: [[双向链接]]]` 交织。
3. **轻量级索引 (`wiki/index.json`)**：Pure-JSON 元数据索引，由 `indexer.py` 全量重建。包含 `nodes`、`weighted_edges`（4-信号相关性权重）、`communities`（Louvain 社区划分）、`community_labels`（动态标签）与 `graph_insights`（拓扑裂缝）。
4. **语义锚点 (`MEMORY/purpose.md`)**：Wiki 的战略目标与关键问题定义，注入每次 LLM 操作以确保方向一致。
5. **审查队列 (`wiki/.meta/review_queue.json`)**：异步人机协作队列与自动捕食任务存储。

## Agentic 编译流

V7.2 采用**两步 CoT + 子代理隔离 + 纵深防御**架构：

* **强制读写节律 (Read-Write Rhythm)**：OODA 循环注入强制的前置实体嗅探 (Entity Sniffing) 与后置的图谱同步 (Graph Sync)，把检索与回写变成系统的“底层呼吸”。
* **两步 Chain-of-Thought**：Step 1 注入 purpose + index summary → LLM 输出结构化分析；Step 2 注入分析结果 + schema → LLM 执行文件写入。物理分离确保"先想清楚再动手"。
* **Subagent 隔离**：`vector-lake-ingestor` / `vector-lake-synthesizer` / `vector-lake-collider` 三个专用 Subagent，被剥夺终端执行权限，强制锁定在文件 I/O。
* **4-信号相关性引擎**：每次索引重建时计算节点间的加权边 (direct link 3.0, source overlap 4.0, Adamic-Adar 1.5, type affinity 1.0)，驱动搜索图扩展和拓扑可视化。
* **Review 闭环**：Agent 在 Step 2 结束时输出 `---REVIEW: type | title---` 块，由 `review.py` 解析存入 JSON 队列，用户异步裁决。
* **强制中文架构**：子代理生成任何报告、实体解析和推演洞察时，强制使用高质量中文输出。
* **Prompt 注入防御**：文件路径注入前经 `_sanitize_for_prompt()` 处理，剥离控制字符、反引号和 `@mention` 劫持向量。
* **写入快照保护**：UPDATE 操作前自动调用 `_backup_wiki_targets()` 创建 `.bak` 快照。

## 核心模块 (Module Map)

| 模块 | 职责 |
|---|---|
| `cli.py` | Argparse CLI 路由入口（sync/search/lint/query/serendipity/graph/review/audit-graph/delete） |
| `tools.py` | 9 个 Tool 函数（含孤岛别名解析、图谱渲染网关等） |
| `ingest.py` | Raw→Wiki 两步 CoT 编译管线（分析→生成 + purpose 注入 + review 解析） |
| `indexer.py` | `index.json` 生成器（4-信号相关性 + Louvain 社区发现 + 动态主题标签 + 强制本体降维） |
| `review.py` | 异步审查队列（REVIEW 块解析、JSON 持久化、CLI 展示/解决） |
| `db.py` | `processed_files.json` 线程安全读写（FileLock） |
| `watchdog_sync.py` | 文件系统实时监听哨兵 |
| `templates/topology.html` | 3D WebGL 力导向拓扑可视化 HTML 模板（带信息抽屉与搜索聚焦） |

## 工作流与命令 (Workflows & Commands)

通过 `gemini` CLI 命令或 `python cli.py <command>` 与 Vector Lake 交互。

### 1. 自动编译管线 (Ingestion Compiler — 2-Step CoT)
当您将新文件放入 `MEMORY/raw/` 目录时，系统唤醒 Agent 执行两步 CoT：先结构化分析（实体、概念、矛盾），再基于分析驱动文件生成、overview 更新和 review 标注。
*   **手动全量编译**: `python cli.py sync`
*   **启动后台监听哨兵**: `python watchdog_sync.py`

### 2. 知识检索 (Search — CJK + Graph Expansion)
CJK bigram 分词 + 三级评分（title +10 / summary +3 / body +1）+ 图扩展（top 3 结果的高权重邻居自动注入候选）。
*   **搜索**: `python cli.py search "DRG 医保" --top_k 5`

### 3. 深度推演落盘 (Query-to-Page — 带预算控制 + 自动回摄)
预算控制的上下文组装（60/20/5/15 分配），推演完成后自动检测新文件、重建索引、为断链生成 seed 页面。
*   **推演查询**: `python cli.py query "对比 Vendor A 和 Vendor B 的 AI 战略差异"`

### 4. 结构化审计自愈 (10-Point Structural Lint)
10 项深度健康体检：前端完整性、命名合规、Type/Status 合法性、Category 词表、重复 ID、Alias 冲突、断链、孤岛页、相似文件名、知识衰变。
*   **审计**: `python cli.py lint [--auto-fix]`

### 5. 审查队列与拓扑审计 (Async Review Queue & Audit-Graph)
查看和处理 LLM 在摄取过程中标记的矛盾、知识空白，以及由图谱算法发现的跨界桥梁（Bridge Nodes）等。
*   **触发图谱审计**: `python cli.py audit-graph` (从索引中抽取网络拓扑裂缝并推入队列)
*   **查看队列**: `python cli.py review`
*   **解决条目**: `python cli.py review resolve <index> [--resolution skip|create]` (若 create 将触发爬虫自动补全网络盲区)

### 6. 3D 拓扑图谱 (Graph View — 动态社区与实体降维)
利用 `weighted_edges` 渲染 3D 交互图谱大屏，支持按本体分类（Entity/Concept）与 Louvain 社区（自动抽取主题词）进行双轨制着色。
*   **大屏展示**: `python cli.py graph`

### 7. 级联删除 (Cascade Delete)
删除原始源文件及其关联的 wiki 页面。
*   **执行**: `python cli.py delete "/path/to/raw/file.pdf"`

## 为什么采用这种架构？

传统的 RAG 系统在每次被提问时都在从零开始重新拼凑知识碎片。而 LLM-Wiki 模式将知识编译一次并保持常新。交叉引用在物理层面上真实存在；事实冲突被显式标注；逻辑综合反映了您阅读过的一切。

V7.2 更是将图谱推入了“自治”阶段。它不再是一个被动查询的静态数据库，而是一个会利用 Louvain 算法侦测自己的“知识盲区”，并主动发起“网络捕食”的生命体。维护一个维基百科所带来的庞大脑力与体力劳动——现在已完全由底层的代码锁、图谱分析与永不疲倦的 LLM 接管。