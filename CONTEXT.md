# CONTEXT: Vector Lake (LLM-Wiki Engine)

## 1. 核心定位 (Status)
**Vector Lake** (V7.1 LLM-Wiki Engine) 是 Gemini CLI 的有状态知识库编译基座。它摒弃了传统的"无状态检索(Stateless RAG)"和黑盒数据库，将长程记忆彻底重构为**基于大模型维护的纯 Markdown 节点网络 (Stateful Compounding Wiki)**。

V7.1 核心升级（借鉴 Karpathy LLM-Wiki 模式与 Garry Tan gbrain）：
*   **双轨制实体模型 (Dual-Track Schema)**: 实体页面物理隔离为液态的“编译共识”与固态的“证据时间线”，彻底消除 LLM 记忆塌缩。
*   **并发安全防御 (FileLock)**: 在所有底层读写（indexer.py & tools.py）中注入跨进程文件锁，彻底解决并发破损（WinError 32）。
*   **检索管线降噪**: 引入前置查询裂变 (Query Expansion) 与后置类型压制 (Type Diversity Capping)，最高限定 Source 类型占 60%，提供立体上下文。
*   **两步 CoT 摄取**: 分析与生成物理分离，先结构化分析再编译写入
*   **4-信号相关性模型**: 直接链接(3.0) + 源重叠(4.0) + Adamic-Adar(1.5) + 类型亲和(1.0)
*   **purpose.md 语义锚点**: 为 Wiki 定义"为什么"，注入每次 LLM 操作
*   **Review 异步审查队列**: LLM 在摄取时标记矛盾/空白/建议，用户异步处理
*   **Token 预算控制**: 60/20/5/15 比例分配，防止上下文溢出
*   **全景 overview.md**: 每次摄取后自动更新的人类可读鸟瞰图

## 2. 物理拓扑 (Architecture)
采用 **Markdown 绝对主权**架构，零外部数据库依赖：
*   **System 1 (Raw Sources)**: `MEMORY/raw/`。不可变的原始信源层（PDF、文章）。
*   **System 2 (The Wiki)**: `MEMORY/wiki/`。大模型拥有绝对读写权限的固态资产层。所有的实体、概念、分析推演均以 `.md` 文件形式存在，通过 `[Relation:: [[双向链接]]]` 相互交织。
*   **System 3 (Lightweight Index)**: `wiki/index.json`。Pure-JSON 元数据索引（含 title/type/aliases/links/sources/summary/weighted_edges），由 `indexer.py` 在 <0.1s 内全量重建，驱动搜索、拓扑可视化与 Agent 寻路。
*   **System 4 (Semantic Anchor)**: `MEMORY/purpose.md`。Wiki 的战略目标与关键问题定义，注入每次 LLM 操作。
*   **System 5 (Review Queue)**: `wiki/.meta/review_queue.json`。异步人机协作队列。

## 3. 核心模块 (Module Map)
| 模块 | 职责 |
|---|---|
| `cli.py` | Argparse 路由入口（含 review/delete 命令） |
| `tools.py` | 8 个 Tool 函数（sync/search/lint/query/serendipity/graph/review/delete） |
| `ingest.py` | Raw→Wiki 两步 CoT 编译管线（分析→生成 + review 解析） |
| `indexer.py` | `index.json` 全量/增量生成器（nodes + links + summary + weighted_edges） |
| `review.py` | 异步审查队列（矛盾/重复/空白/建议的解析、存储、展示） |
| `db.py` | `processed_files.json` 读写（MD5 哈希去重） |
| `watchdog_sync.py` | 文件系统实时哨兵 |
| `templates/` | 3D 拓扑可视化 HTML 模板 |
| `agents/` | 3 个 Gemini Subagent（ingestor/synthesizer/collider） |

## 4. 交互协议 (Interaction Protocol - CLI Native)
**`[HARD_LOCK]`** 所有交互必须通过 `run_shell_command` 调用底层 CLI 工具执行。

### 核心 Shell 指令集：
*   **🔄 自动编译管线 (Ingest Compiler)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\watchdog_sync.py` (后台运行监听 `raw/` 目录)
*   **🟢 手动全量编译 (Sync — 2-Step CoT)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py sync`
*   **🟡 知识库自愈审计 (Lint)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py lint [--auto-fix]` (执行10项结构性检查及强制脱水重写)
*   **🧠 深度推演落盘 (Query-to-Page — 带预算控制)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py query "推演指令" [--dry-run]` (生成高密度回复并物理写入，`--dry-run`仅输出到命令行)
*   **🔍 知识检索 (Search — CJK 分词 + 图扩展)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py search "关键词" [--top_k 5] [--domain] [--cluster] [--include-history]` (基于索引多维打分，支持领域过滤与弃用展示)
*   **🌐 3D 拓扑图谱 (Graph — 带相关性权重)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py graph`
*   **📋 审查队列 (Review — 异步人机协作)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py review` (列出待处理项)
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py review resolve <index> [--resolution skip|create]` (解决特定审查项)
*   **🗑️ 级联删除 (Delete — 源文件清理)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py delete "<raw文件路径>" [--dry-run]` (删除原始源及其关联 wiki 页面)

## 5. 运行守则 (Runtime Rules)
1.  **资产化原则**: 每当进行复杂的多文件对比或逻辑推演时，必须优先使用 `query` 命令，让其洞察过程固化为物理 Markdown 文件。
2.  **Schema 纪律**: 大模型在更新 Wiki 页面时，必须严格遵守 `schema.md` 中的 YAML Frontmatter（包括 domain, topic_cluster 等必填字段）和 `[Relation:: [[双向链接]]]` 规范。
3.  **安全纪律**: 文件路径在注入 prompt 前自动 sanitize（控制字符、反引号、@mention），UPDATE 操作前自动创建 `.bak` 快照。
4.  **强制中文与无塑化**: 所有由 Agent 撰写或生成的洞察内容，必须强制使用结构化高质量中文，严禁机翻腔与 AI 八股文。
5.  **Purpose 对齐**: 所有 LLM 操作（Ingest/Query/Lint）均注入 `purpose.md` 上下文，确保知识库方向一致。