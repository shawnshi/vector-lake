# CONTEXT: Vector Lake (LLM-Wiki Engine)

## 1. 核心定位 (Status)
**Vector Lake** (V7.0 LLM-Wiki Engine) 是 Gemini CLI 的有状态知识库编译基座。它摒弃了传统的"无状态检索(Stateless RAG)"和黑盒数据库，将长程记忆彻底重构为**基于大模型维护的纯 Markdown 节点网络 (Stateful Compounding Wiki)**。

## 2. 物理拓扑 (Architecture)
采用 **Markdown 绝对主权**架构，零外部数据库依赖：
*   **System 1 (Raw Sources)**: `MEMORY/raw/`。不可变的原始信源层（PDF、文章）。
*   **System 2 (The Wiki)**: `MEMORY/wiki/`。大模型拥有绝对读写权限的固态资产层。所有的实体、概念、分析推演均以 `.md` 文件形式存在，通过 `[Relation:: [[双向链接]]]` 相互交织。
*   **System 3 (Lightweight Index)**: `wiki/index.json`。Pure-JSON 元数据索引（含 title/type/aliases/links/summary），由 `indexer.py` 在 <0.1s 内全量重建，驱动搜索、拓扑可视化与 Agent 寻路。

## 3. 核心模块 (Module Map)
| 模块 | 职责 |
|---|---|
| `cli.py` | Argparse 路由入口 |
| `tools.py` | 6 个 Tool 函数（sync/search/lint/query/serendipity/graph） |
| `ingest.py` | Raw→Wiki 编译管线（含 prompt 注入防御 + `.bak` 快照） |
| `indexer.py` | `index.json` 全量/增量生成器（nodes + links + summary） |
| `db.py` | `processed_files.json` 读写（MD5 哈希去重） |
| `watchdog_sync.py` | 文件系统实时哨兵 |
| `templates/` | 3D 拓扑可视化 HTML 模板 |
| `agents/` | 3 个 Gemini Subagent（ingestor/synthesizer/collider） |

## 4. 交互协议 (Interaction Protocol - CLI Native)
**`[HARD_LOCK]`** 所有交互必须通过 `run_shell_command` 调用底层 CLI 工具执行。

### 核心 Shell 指令集：
*   **🔄 自动编译管线 (Ingest Compiler)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\watchdog_sync.py` (后台运行监听 `raw/` 目录)
*   **🟢 手动全量编译 (Sync)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py sync`
*   **🟡 知识库自愈审计 (Lint)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py lint` (执行重复实体合并与矛盾审查)
*   **🧠 深度推演落盘 (Query-to-Page)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py query "推演指令"` (生成高密度回复，并将洞察写入 `wiki/` 成为新节点)
*   **🔍 知识检索 (Search)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py search "关键词"` (基于 index.json 元数据 + summary 全文匹配)
*   **🌐 3D 拓扑图谱 (Graph)**
    `python C:\Users\shich\.gemini\extensions\vector-lake\cli.py graph`

## 5. 运行守则 (Runtime Rules)
1.  **资产化原则**: 每当进行复杂的多文件对比或逻辑推演时，必须优先使用 `query` 命令，让其洞察过程固化为物理 Markdown 文件。
2.  **Schema 纪律**: 大模型在更新 Wiki 页面时，必须严格遵守 `schema.md` 中的 YAML Frontmatter 和 `[Relation:: [[双向链接]]]` 规范。
3.  **安全纪律**: 文件路径在注入 prompt 前自动 sanitize（控制字符、反引号、@mention），UPDATE 操作前自动创建 `.bak` 快照。