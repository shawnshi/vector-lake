# Vector Lake (LLM-Wiki 引擎 - Agentic Computable-Graph V6.0)

**Vector Lake** (V6.0) 是 Gemini CLI 的有状态、可产生复利的知识图谱与语义编译引擎。

它实现了由 Andrej Karpathy 提出的 [LLM-Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，彻底摒弃了传统的“无状态检索增强生成 (RAG)”和黑盒数据库。取而代之的是，它依赖 Gemini CLI Native Agent 来主动编译、相互链接并维护一个纯 Markdown 文件组成的持久化网络。

## 物理架构 (Architecture)

系统在三个截然不同的物理层面上运行：

1. **不可变信源区 (`MEMORY/raw/`)**：您收集的原始数据（PDF、文章、日志）。Agent 会从中读取信息，但**绝对不会**修改该目录下的任何文件。
2. **活跃的知识库 (`MEMORY/wiki/`)**：持久化的逻辑资产区。Agent 在这里拥有绝对的写权限。当摄入新信源或回答新问题时，Agent 会利用其自带的 `write_file` 和 `replace` 工具，在此创建实体、概念、综合推演页面，并更新双向链接 (`[[Link]]`)。
3. **轻量级索引 (`wiki/index.json`)**：Pure-JSON 元数据索引，由 `indexer.py` 在每次 sync 时在 <0.1s 内全量重建。驱动 3D 拓扑可视化与 Agent 寻路。

## Agentic 编译流 (The V6.0 Shift)
与之前的版本通过调用通用泛型大模型不同，**V6.0 彻底转向了独立子代理 (Subagent) 和物理隔离架构**：
* 后台通过 `vector-lake-ingestor` 专用 Subagent 进行处理，该代理被剥夺了任何终端执行权限，强制锁定在文件 I/O，规避了 YOLO 模式的致命风险。
* `schema.md` 中的格式规则、元数据字段集、[Relation:: [[ID]]] 生成规则已被全量内聚入子代理的 DNA (system.md)，大幅收敛上下文推理所需的 Tokens。

## 核心元文件 (Core Files)

*   **`wiki/index.json`**：极速检索缓存。取代了脆弱的单体 `index.md`，由后台监听器做 O(1) 的局部更新，保障 Agent 寻路不迷失。
*   **`wiki/log.md`**：按时间顺序追加的操作流水日志，记录 Agent 执行过的所有动作。
*   **`schema.md` / `SCHEMA_CATEGORIES.md`**：系统治理协议的呈现。包含子代理所固化的 YAML V6.0 元数据、双向链接范式等。

## 工作流与命令 (Workflows & Commands)

您可以通过 `gmni` 或 `gemini` CLI 命令行与 Vector Lake 进行交互。

### 1. 自动编译管线 (Ingestion Compiler)
当您将新文件放入 `MEMORY/raw/` 目录时，系统会唤醒 Agent 读取它们、提取关键信息，并将其整合到现有的 Wiki 中——它会更新相关的实体页、重写摘要、标注矛盾点并更新全局索引。

*   **手动全量编译**: `gemini sync_vector_lake`
*   **启动后台监听哨兵**: `gemini watchdog_sync`

### 2. 深度推演落盘 (Tacit Query-to-Page)
系统会在 `index.json` 中查找关联节点，然后在文件系统中抽取它们基于 `[Relation:: [[ID]]]` 的真实物理拓扑邻居，组装为“默会图谱上下文 (Tacit Subgraph Context)”，再抛给模型进行无偏见合成。

*   **推演查询**: `gemini query_logic_lake "对比一下 Vendor A 和 Vendor B 的 AI 战略差异"`

### 3. 结构化审计自愈 (Structural Linting & Decay)
周期性地对 Wiki 进行健康体检。系统会扫描孤儿页面、重复实体，并自动执行 AutoDream 脱水重写（针对超过 60 天处于 sprouting 状态的节点强制写入 `decayed: true`）。

*   **审计清理**: `gemini run_wiki_lint` (调用代码底层的 `--auto-fix`)

### 4. 知识检索 (Search)
使用 `index.json` 元数据索引在已编译的 Markdown 页面中进行结构化检索。

*   **搜索**: `gemini search_vector_lake "边缘计算模型"`

### 5. 3D 拓扑图谱 (Graph View)
解析纯 Markdown 节点网络，提取 `[[双向链接]]` 并将其渲染为无需依赖图数据库的、轻量级、可交互的 3D 力导向关系图大屏。

*   **大屏展示**: `gemini show_graph`

## 为什么采用这种架构？ (Why this approach?)
传统的 RAG 系统在每次被提问时都在从零开始重新拼凑知识碎片。而 LLM-Wiki 模式将知识编译一次并保持常新。交叉引用在物理层面上真实存在；事实冲突会被显式标注；逻辑综合反映了您阅读过的一切。维护一个维基百科所带来的记账、打标签、更新链接的庞大“脑力体力活”——现在已完全外包给了永不疲倦的 LLM。全量索引重建在 <0.1s 内完成，无需外部向量数据库，确保系统自主可控。