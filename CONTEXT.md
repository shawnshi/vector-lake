# CONTEXT: Vector Lake (LLM-Wiki Engine)

## 1. 核心定位 (Status)
**Vector Lake** (V8 Hardened Kernel) 是 Gemini CLI 的有状态知识库编译基座。它摒弃了传统的"无状态检索(Stateless RAG)"和黑盒数据库，将长程记忆彻底重构为**基于大模型维护的纯 Markdown 节点网络 (Stateful Compounding Wiki)**。

V8 核心升级（The Topology & Autonomous Expansion Edition）：
*   **反漂移力场与前置防腐 (V8 Kernel)**: 引入 `alignment_score` 和 Shift-Left Entity Resolution 前置同态字典，高频截胡大模型的幻觉与变种命名。
*   **拓扑审计与图谱裂缝发现 (Louvain Integration)**: 在 `vector_lake/indexer.py` 环节原生集成 Louvain 算法，自动划分动态知识社区（Community Detection），并为其动态提取主题标签（如 `Comm 2: Agentic AI / Vector Lake`）。算法同时计算内聚度 (Cohesion Score)，侦测孤立节点、稀疏知识区与跨域桥接节点。
*   **自动捕食闭环 (Autonomous Deep Research)**: 引入 `audit-graph` 指令。系统自动将发现的拓扑裂缝合成为预定义研究任务注入 `review_queue.json`。人类批准（resolve create）后，系统自动触发网络爬虫获取资料并执行两步 CoT 回摄，实现知识边界的自主扩张。
*   **高并发免疫与级联降级 (429 Immunity Cascade)**: 彻底重构 Ingestion 引擎的 LLM 通信层，免疫底层 API 耗尽（429 报错）导致的进程静默假死假象。拦截底层 stderr 以越过 Exit 0 欺骗，实现顺滑的模型阶梯降维打击。
*   **3D 拓扑视觉引擎 (ForceGraph3D)**: 重新引入 3D 力导向球形渲染架构，同时保留并升级了所有高级 UI：节点度数缩放、双轨制着色切换（本体类型 vs. 拓扑社区）、带类别徽章（Category Badges）的悬停高亮 Ego-Graph、以及相机自动拉近的全局搜索框。
*   **严酷本体论降维 (Strict Ontology Coercion)**: 在代码层级将所有节点强制拍扁、归一化为四种核心基础类型：`Entity`, `Concept`, `Source`, `Synthesis`。系统自动拦截并纠正 LLM 产生的命名幻觉（如 `Project`、`Person`、`System` 等），保障大盘统计的绝对纯净。
*   **别名解析与孤岛过滤 (Orphan Link Resolution)**: 图谱渲染引擎自动将前端别名（Aliases）映射到真实的物理节点，并静默过滤掉指向不存在文件的空链（Orphan Links），彻底消除了前端图谱中出现的 `[UNKNOWN]` 节点。
*   **双轨制实体模型 (Dual-Track Schema)**: 实体页面物理隔离为液态的“编译共识”与固态的“证据时间线”，彻底消除 LLM 记忆塌缩。
*   **并发安全防御 (FileLock)**: 在所有底层读写中注入跨进程文件锁，彻底解决并发破损（WinError 32）。

## 2. 物理拓扑 (Architecture)
采用 **Markdown 绝对主权**架构，零外部数据库依赖：
*   **System 1 (Raw Sources)**: `MEMORY/raw/`。不可变的原始信源层（PDF、文章）。
*   **System 2 (The Wiki)**: `MEMORY/wiki/`。大模型拥有绝对读写权限的固态资产层。所有的实体、概念、分析推演均以 `.md` 文件形式存在，通过 `[Relation:: [[双向链接]]]` 相互交织。
*   **System 3 (Lightweight Index)**: `wiki/index.json`。Pure-JSON 元数据索引，由 `vector_lake/indexer.py` 全量重建，驱动搜索、拓扑可视化与 Agent 寻路。
*   **System 4 (Semantic Anchor)**: `MEMORY/purpose.md`。Wiki 的战略目标与关键问题定义。
*   **System 5 (Review Queue)**: `wiki/.meta/review_queue.json`。异步人机协作与智能体任务分发队列。

## 3. 核心模块 (Module Map)
| 模块 | 职责 |
|---|---|
| `cli.py` | 根目录薄入口 |
| `vector_lake/tools.py` | Tool facade（sync/search/lint/query/graph/review/gc/delete/audit_graph等） |
| `vector_lake/ingest.py` | 基于 Python-Led I/O 的两步 CoT 编译管线 |
| `vector_lake/indexer.py` | `index.json` 生成器（含 Louvain 社区划分、4 类本体强制降维） |
| `vector_lake/tool_review.py` | 异步审查队列与 Web Grounding 触发器 |
| `vector_lake/tool_gc.py` | 自动化修剪低度数长尾孤岛节点 |
| `vector_lake/governance_store.py` | V8 统一事实引擎主控 |
| `watchdog_sync.py` | 常驻后台的增量编译监控守护进程 |
| `templates/` | 3D 球形拓扑可视化 HTML 模板（ForceGraph3D） |
| `agents/` | 2 个 Gemini Subagent（ingestor / synthesizer），已切换至纯输出态 |

## 4. 交互协议 (Interaction Protocol - CLI Native)
**`[HARD_LOCK]`** 所有交互必须通过 `run_shell_command` 调用底层 CLI 工具执行。

### 核心 Shell 指令集：
*   **🔄 自动编译管线 (Ingest Compiler)**
    `python watchdog_sync.py`
*   **🟢 手动全量编译 (Sync)**
    `python cli.py sync`
*   **🟡 知识库自愈审计 (Lint)**
    `python cli.py lint [--auto-fix]`
*   **🧠 深度推演落盘 (Query-to-Page)**
    `python cli.py query "推演指令" [--dry-run]`
*   **🔍 知识检索 (Search)**
    `python cli.py search "关键词" [--top_k 5]`
*   **🌐 3D 拓扑图谱 (Graph)**
    `python cli.py graph`
*   **📋 审查队列与自动化捕食 (Review & Grounding)**
    `python cli.py audit-graph` (抽取 Louvain 裂缝注入队列)
    `python cli.py review`
    `python cli.py review resolve <index> [--resolution skip|create]`
    `python cli.py review ground <index>` (一键触发 Google Search 全自动知识入湖)
*   **🗑️ 级联删除与修剪 (Delete & GC)**
    `python cli.py gc --days 30` (修剪长尾孤岛节点)
    `python cli.py delete "<raw文件路径>"`
*   **🛠️ 治理与系统运维 (Governance & Ops)**
    `python cli.py doctor` (运行时环境与文件物理阵地体检)
    `python cli.py debt` (输出系统混乱度债务与治理指标)
    `python cli.py trace "<查询或ID>"` (全链路溯源归因审计)
    `python cli.py merge-suggestions` (自动探测并生成实体归并候选清单)

## 5. 运行守则 (Runtime Rules)
1.  **资产化原则**: 每当进行复杂的多文件对比或逻辑推演时，必须优先使用 `query` 命令，让其洞察过程固化为物理 Markdown 文件。
2.  **Schema 纪律**: 必须严格遵守 `schema.md`。类型 (`type`) 被严格限制为 `Entity`, `Concept`, `Source`, `Synthesis`。
3.  **安全纪律**: 文件路径自动 sanitize，UPDATE 自动创建 `.bak` 快照。
4.  **强制中文与无塑化**: 必须强制使用结构化高质量中文，严禁机翻腔与 AI 八股文。
5.  **Purpose 对齐**: 所有 LLM 操作均注入 `purpose.md` 上下文。
