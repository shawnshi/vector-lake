# Vector Lake

Vector Lake 是一个运行在 Gemini CLI 扩展环境中的 Markdown 知识图谱编译器。它保留了 V7.2 的 page-centric LLM-Wiki 工作流，同时已经接入一层最小可运行的 V8 知识治理内核：`Entity / Claim / Evidence / Source / Change Set`。

它不是传统 RAG 服务，也不依赖外部数据库。主存仍然是 `MEMORY/raw` 和 `MEMORY/wiki`，但关键身份、provenance、validity 和 publish 状态已经进入 canonical object store。

## 当前状态

- V7.2 兼容命令仍可用：`sync`、`search`、`query`、`lint`、`graph`、`review`、`audit-graph`、`delete`
- V8 治理命令已落地：`doctor`、`migrate-v8`、`publish`、`debt`、`trace`、`merge-suggestions`
- `query --dry-run` 已实现为真正不落盘的预览模式
- 图谱支持双视图：
  `Page View` 用于传统 wiki node topology
  `Claim View` 用于 claim-level governance graph

## 核心能力

- Markdown-first 知识库：原始信源在 `MEMORY/raw/`，发布页面在 `MEMORY/wiki/`
- Canonical governance store：实体、主张、证据、来源和变更集保存在 `.meta/*.json`
- Provenance trace：query 和 trace 可以追到 claim、source page、locator 和 validity state
- Candidate publish flow：ingest / query 会生成 change set，publish 负责把 canonical 变更正式落地
- Governance debt：系统会计算 `review-due`、`expired`、`unsupported`、`conflicted`、merge candidates 等知识债务
- Dual graph UI：3D 拓扑图支持 page graph / claim graph 切换
- FileLock safety：索引和队列写入都有锁，避免 Windows 下并发写坏

## 存储结构

```text
MEMORY/
  raw/
  wiki/
    *.md
    index.json
    views/
      *.md
      open_questions.md
    .meta/
      entities.json
      claims.json
      evidence.json
      sources.json
      change_sets.json
      governance_queue.json
      alias_registry.json
```

如果 `MEMORY/wiki/.meta` 不可写，系统会自动回退到仓库内的 `data/v8_meta/`。这让 Vector Lake 可以在只读 wiki 元目录环境下继续运行。

## 运行模型

### 1. Ingest

`sync` 或 `watchdog_sync.py` 会扫描 `MEMORY/raw/`，通过两步式流程把原始信源编译进 wiki。当前兼容层仍保留 V7 风格页面写入，但在写完页面后会同步 canonical store。

### 2. Query

`query` 会先组装上下文，再调用 synthesizer。非 dry-run 模式下：

- 检测新建或更新的 wiki 页面
- sanitize 页面
- 重建索引
- 同步 canonical store
- 附加 provenance trace

### 3. Publish

`publish` 会发布 pending change sets，并重建 canonical markdown views。当前 view builder 已支持：

- entity canonical view
- `open_questions.md` debt view

### 4. Governance

V8 治理层会把 claim 解析为 runtime validity states，例如：

- `active`
- `review-due`
- `needs-review`
- `expiring-soon`
- `expired`
- `unsupported`
- `conflicted`
- `provisional`

## 命令

### 基础工作流

- 全量编译：`python cli.py sync`
- 后台监听：`python watchdog_sync.py`
- 搜索：`python cli.py search "Agentic workflow" --top_k 5`
- 推演：`python cli.py query "对比两个方案的知识治理路径"`
- 推演预览：`python cli.py query "生成一版提纲" --dry-run`
- 审计：`python cli.py lint`
- 图谱：`python cli.py graph`
- 图谱洞察入队：`python cli.py audit-graph`
- 查看 review 队列：`python cli.py review`
- 删除 raw 及关联 wiki：`python cli.py delete "C:\\path\\to\\raw\\file.pdf"`

### V8 治理命令

- 环境检查：`python cli.py doctor`
- 迁移现有 wiki 到 canonical store：`python cli.py migrate-v8`
- 迁移预览：`python cli.py migrate-v8 --dry-run`
- 发布 pending change sets：`python cli.py publish --limit 10`
- 查看知识债务：`python cli.py debt --top 20`
- 查看可追溯链路：`python cli.py trace "Agentic"`
- 查看 merge 建议：`python cli.py merge-suggestions --limit 20`
- 仅预览 merge 建议：`python cli.py merge-suggestions --limit 20 --preview`

## 主要模块

| 模块 | 职责 |
|---|---|
| `cli.py` | CLI 路由入口 |
| `tools.py` | Tool facade，聚合各命令模块 |
| `ingest.py` | raw -> wiki 编译流程 |
| `indexer.py` | 生成 `index.json`，维护 page graph + claim graph 投影 |
| `review.py` | review queue 与治理队列兼容层 |
| `wiki_utils.py` | 路径、frontmatter、atomic write、meta store helper |
| `governance_store.py` | canonical object store 读写 |
| `claim_extractor.py` | page/block -> claim/evidence/source 抽取 |
| `change_sets.py` | change set 创建与发布包装 |
| `governance_metrics.py` | validity 推理与 debt metrics |
| `provenance.py` | provenance trace 构建 |
| `view_builder.py` | canonical objects -> markdown views |
| `tool_graph.py` | 3D 图谱渲染与 audit-graph |
| `templates/topology.html` | page/claim 双视图 WebGL 模板 |

## 图谱说明

`graph` 命令会打开一个本地 HTML dashboard，当前支持：

- `Page View`
  传统 wiki 页面节点图
- `Claim View`
  claim-level 节点图，按 validity state 着色
- `Ontology / Claim State` 着色
- `Page Community` 着色
- governance metrics HUD

## 测试与验证

当前仓库内有回归测试：

- `python -m unittest discover -s tests -p "test_*.py" -v`

覆盖的重点包括：

- `query --dry-run` 不落盘
- `delete_source` 安全语义
- review queue 并发写
- multiline YAML source parsing
- 索引脏标记与恢复路径
- V8 migration / publish / debt / trace
- block-level claim extraction
- validity state inference
- claim graph projection

## 依赖

最小运行依赖在 [requirements.txt](requirements.txt)：

- `filelock`
- `networkx`
- `python-dotenv`
- `python-louvain`
- `PyYAML`
- `watchdog`

## 版本说明

仓库实现已经包含 V8 治理层，但 `gemini-extension.json` 仍沿用 `7.2.0` 的扩展版本号。可以把当前状态理解为：

- 运行时兼容层：V7.2
- 知识治理内核：V8 minimal runnable implementation

## 设计原则

- 不引入外部数据库作为主存
- Markdown 仍然是人类可读、可审计的发布层
- canonical objects 才是身份、有效期和 provenance 的主权层
- 能兼容旧工作流时，不破坏旧命令

## 相关文档

- V8 规范：[docs/specs/2026-04-19-v8-knowledge-governance-spec.md](docs/specs/2026-04-19-v8-knowledge-governance-spec.md)
