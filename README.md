# Vector Lake (LLM-Wiki 引擎 - Native Agent Edition)

**Vector Lake** (V4.1) 是 Gemini CLI 的有状态、可产生复利的知识库引擎。

它实现了由 Andrej Karpathy 提出的 [LLM-Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，彻底摒弃了传统的“无状态检索增强生成 (RAG)”和黑盒数据库。取而代之的是，它依赖 Gemini CLI Native Agent 来主动编译、相互链接并维护一个纯 Markdown 文件组成的持久化网络。

## 物理架构 (Architecture)

系统在三个截然不同的物理层面上运行：

1. **不可变信源区 (`MEMORY/raw/`)**：您收集的原始数据（PDF、文章、日志）。Agent 会从中读取信息，但**绝对不会**修改该目录下的任何文件。
2. **活跃的知识库 (`MEMORY/wiki/`)**：持久化的逻辑资产区。Agent 在这里拥有绝对的写权限。当摄入新信源或回答新问题时，Agent 会利用其自带的 `write_file` 和 `replace` 工具，在此创建实体、概念、综合推演页面，并更新双向链接 (`[[Link]]`)。
3. **语义索引 (`data/vector_lake_db`)**：一个轻量级的 ChromaDB 实例，严格降级为只读的语义搜索引擎。它的唯一作用是帮助 Agent 在海量的 Markdown Wiki 页面中快速进行空间定位。

## Native Agent 编译流 (The V4.1 Shift)
与之前的版本通过 Python SDK 直接调用 Google API 不同，**V4.1 彻底转向了 Native Agent 架构**：
* Python 脚本（`ingest.py` / `watchdog_sync.py`）不再处理 JSON 解析，也不再直接调用模型。
* 它们通过 `subprocess` 在后台唤醒一个无头模式 (Headless) 的 Gemini CLI Agent (带 `--yolo` 参数)。
* 这种架构完美继承了您本地的网络代理（解决了 API 区域限制），并且赋予了编译流水线调用子代理（如 `generalist`）处理超长文献的能力。

## 核心元文件 (Core Files)

*   **`wiki/index.md`**：内容检索目录。Agent 在每次注入编译后都会更新这份实体、概念和信源的清单。
*   **`wiki/log.md`**：按时间顺序追加的操作流水日志，记录 Agent 执行过的所有注入 (Ingest)、查询 (Query) 或审计 (Lint) 动作。
*   **`schema.md`**：系统治理协议。该文件规定了 Agent 在编写 Wiki 时必须遵守的格式纪律（YAML 元数据、双向链接规范、如何处理事实冲突）。

## 工作流与命令 (Workflows & Commands)

您可以通过 `gmni` 或 `gemini` CLI 命令行与 Vector Lake 进行交互。

### 1. 自动编译管线 (Ingestion Compiler)
当您将新文件放入 `MEMORY/raw/` 目录时，系统会唤醒 Agent 读取它们、提取关键信息，并将其整合到现有的 Wiki 中——它会更新相关的实体页、重写摘要、标注矛盾点并更新全局索引。

*   **手动全量编译**: `gemini sync_vector_lake`
*   **启动后台监听哨兵**: `gemini watchdog_sync`

### 2. 深度推演落盘 (Query-to-Page)
不同于阅后即焚的聊天对话，高价值的综合推演问题将基于 Wiki 进行评估。如果 Agent 生成了全新的洞察或对比分析，它会强制要求自己将这份高质量答案作为新的 Markdown 页面写入 `wiki/` 目录中。

*   **推演查询**: `gemini query_logic_lake "对比一下 Vendor A 和 Vendor B 的 AI 战略差异"`

### 3. 结构化审计自愈 (Structural Linting)
周期性地对 Wiki 进行健康体检。Agent 会扫描孤儿页面、重复实体以及过时的声明，并自动对它们进行合并与重构。

*   **审计清理**: `gemini run_wiki_lint`

### 4. 语义空间检索 (Semantic Search)
使用 ChromaDB 向量索引在已编译的 Markdown 页面中进行快速的语义搜索。

*   **搜索**: `gemini search_vector_lake "边缘计算模型"`

### 5. 3D 拓扑图谱 (Graph View)
解析纯 Markdown 节点网络，提取 `[[双向链接]]` 并将其渲染为无需依赖图数据库的、轻量级、可交互的 3D 力导向关系图大屏。

*   **大屏展示**: `gemini show_graph`

## 为什么采用这种架构？ (Why this approach?)
传统的 RAG 系统在每次被提问时都在从零开始重新拼凑知识碎片。而 LLM-Wiki 模式将知识编译一次并保持常新。交叉引用在物理层面上真实存在；事实冲突会被显式标注；逻辑综合反映了您阅读过的一切。维护一个维基百科所带来的记账、打标签、更新链接的庞大“脑力体力活”——现在已完全外包给了永不疲倦的 LLM。