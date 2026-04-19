# Vector Lake

Vector Lake 是一个运行在 Gemini 扩展环境里的 Markdown-first 知识编译器。

它保留了 V7 的 page-centric wiki 工作流，同时已经接入一层可运行的 V8 治理内核：`Entity / Claim / Evidence / Source / Change Set`。项目的目标不是做一个外部数据库驱动的 RAG 服务，而是把 `MEMORY/raw` 中的原始资料持续编译为可搜索、可追溯、可治理的知识资产。

## 项目定位

- 主存仍然是文件系统，而不是外部数据库
- `MEMORY/raw` 是原始信源层
- `MEMORY/wiki` 是发布层和人类可读层
- `.meta/*.json` 是 canonical governance store
- `index.json` 是轻量 page-centric 运行时索引
- `claim_graph.json` 是单独拆分的 claim graph 投影，不再塞进主索引

一句话说，Vector Lake 现在是一个带治理层的 Markdown knowledge compiler，而不是传统意义上的检索问答后端。

## 当前能力

- `sync` / `watchdog`：把 raw sources 编译成 wiki 页面
- `search`：基于 page index 做 CJK-aware 搜索和图扩展
- `query`：组装上下文并调用 synthesizer 生成推演结果
- `graph`：渲染 page view / claim view 双视图拓扑
- `review`：统一查看 legacy review queue 和 V8 governance queue
- `audit-graph`：把拓扑洞察转成待处理治理项
- `migrate-v8`：把现有 wiki 回填进 V8 canonical store
- `publish`：发布 pending change sets 并重建 canonical markdown views
- `debt`：输出治理债务指标
- `trace`：查看 claim/source/provenance 链路
- `merge-suggestions`：生成或入队实体归并候选
- `delete`：级联清理 raw source 及关联 wiki 资产
- `doctor`：检查依赖、目录和运行环境

## 架构概览

```text
MEMORY/raw
  -> ingest
  -> wiki pages
  -> canonical governance store
  -> runtime indexes
  -> query / graph / review / publish
```

更具体地说：

1. `sync` 或 `watchdog_sync.py` 扫描 `MEMORY/raw`
2. `ingest` 调用 `vector-lake-ingestor` 子代理生成或更新 wiki 页面
3. 页面写入后，同步 canonical objects 到 `.meta/*.json`
4. `indexer` 生成轻量 `index.json`，并单独写 `claim_graph.json`
5. `query`、`graph`、`review`、`debt`、`trace` 读取这些投影或 canonical store 提供运行时能力

## 存储结构

```text
MEMORY/
  raw/
  wiki/
    *.md
    index.json
    claim_graph.json
    views/
      *.md
      open_questions.md
    .meta/
      entities.json
      claims.json
      evidence.json
      sources.json
      alias_registry.json
      change_sets.json
      governance_queue.json
      review_queue.json
```

说明：

- `index.json` 现在只保留 page-centric 和轻量治理投影
- `claim_graph.json` 独立存储 claim graph，避免主索引膨胀
- 如果 `MEMORY/wiki/.meta` 不可写，系统会自动回退到仓库内的 `data/v8_meta/`

## 运行模型

### Ingest

`sync` 和 `watchdog_sync.py` 负责从 `MEMORY/raw` 到 `MEMORY/wiki` 的编译链。

当前仍兼容 V7 风格的页面输出，但每次成功写页后都会同步到 V8 canonical store。

### Query

`query` 的执行路径是：

1. 组装搜索上下文
2. 调用 `vector-lake-synthesizer`
3. 非 `--dry-run` 模式下检测新建或更新的 wiki 页面
4. sanitize 节点
5. 重建索引
6. 同步 canonical store
7. 追加 provenance trace

`query --dry-run` 现在是真正的不落盘预览。

### Governance

V8 治理层管理这些对象：

- `Entity`
- `Claim`
- `Evidence`
- `Source`
- `Change Set`

同时会推导 claim 的 runtime validity state，例如：

- `active`
- `review-due`
- `needs-review`
- `expiring-soon`
- `expired`
- `unsupported`
- `conflicted`
- `provisional`

### Review

`review` 已经不是只看旧版 async queue。

它现在统一展示两条待处理通道：

- legacy `review_queue.json`
- V8 `governance_queue.json`

统一视图具备这些行为：

- 显示稳定 `item_id`
- 标明队列来源
- 对超长 `search_queries` 和 `affected_pages` 自动摘要
- 支持按可见序号或稳定 `item_id` resolve

示例：

- `python cli.py review`
- `python cli.py review resolve 0`
- `python cli.py review resolve review_ab12cd34ef56`
- `python cli.py review resolve gov_ab12cd34ef56 --resolution skip`

### Publish

`publish` 会发布 pending change sets，并重建 canonical markdown views。

当前默认会生成：

- entity canonical views
- `open_questions.md`

## 命令面

CLI 是主入口。Gemini 扩展侧的命令壳定义在 `commands/`，但真正的运行语义以 `python cli.py ...` 为准。

### 核心工作流

- 全量编译：`python cli.py sync`
- 后台监听：`python watchdog_sync.py`
- 搜索：`python cli.py search "边缘计算模型" --top_k 5`
- 推演：`python cli.py query "对比 Vendor A 和 Vendor B 的 AI 战略差异"`
- 推演预览：`python cli.py query "生成一版提纲" --dry-run`
- 审计：`python cli.py lint`
- 图谱：`python cli.py graph`
- 图谱洞察入队：`python cli.py audit-graph`
- 查看 review：`python cli.py review`
- 删除 source：`python cli.py delete "C:\\path\\to\\raw\\file.pdf"`

### 治理工作流

- 环境检查：`python cli.py doctor`
- 回填 canonical store：`python cli.py migrate-v8`
- 回填预览：`python cli.py migrate-v8 --dry-run`
- 发布 change sets：`python cli.py publish --limit 10`
- 查看治理债务：`python cli.py debt --top 20`
- 查看 provenance：`python cli.py trace "Agentic"`
- 归并建议预览：`python cli.py merge-suggestions --limit 20 --preview`
- 归并建议入队：`python cli.py merge-suggestions --limit 20`

### Gemini 命令壳

`commands/` 已按三层分组：

- core workflow
- governance workflow
- background services

详见 [commands/README.md](commands/README.md)。

## 主要模块

| 模块 | 说明 |
|---|---|
| [cli.py](cli.py) | 根目录薄入口，转发到 `vector_lake.cli_app` |
| [watchdog_sync.py](watchdog_sync.py) | 根目录薄入口，转发到 `vector_lake.watchdog_app` |
| [agents/vector-lake-ingestor](agents/vector-lake-ingestor) | ingest 使用的 Gemini 子代理 |
| [agents/vector-lake-synthesizer](agents/vector-lake-synthesizer) | query synthesis 使用的 Gemini 子代理 |
| [vector_lake/ingest.py](vector_lake/ingest.py) | raw -> wiki 编译流程 |
| [vector_lake/indexer.py](vector_lake/indexer.py) | 生成 `index.json` 和 `claim_graph.json` |
| [vector_lake/governance_store.py](vector_lake/governance_store.py) | canonical object store |
| [vector_lake/review.py](vector_lake/review.py) | legacy review queue、稳定 `item_id`、去重、resolve 兼容 |
| [vector_lake/tool_review.py](vector_lake/tool_review.py) | 统一 review 视图和 resolve 路由 |
| [vector_lake/claim_extractor.py](vector_lake/claim_extractor.py) | page/block -> claim/evidence 抽取 |
| [vector_lake/provenance.py](vector_lake/provenance.py) | provenance trace 构建 |
| [vector_lake/governance_metrics.py](vector_lake/governance_metrics.py) | validity 推理和 debt 计算 |
| [vector_lake/view_builder.py](vector_lake/view_builder.py) | canonical objects -> markdown views |
| [vector_lake/tools.py](vector_lake/tools.py) | tool facade |
| [templates/topology.html](templates/topology.html) | page/claim 双视图图谱模板 |

## 图谱与索引

`graph` 会渲染一个本地 HTML dashboard，当前支持：

- `Page View`
- `Claim View`
- ontology / claim-state 着色
- page community 着色
- governance metrics HUD

索引层现在有一个重要变化：

- `index.json` 已瘦身为主运行索引
- `claim_graph.json` 独立承载 claim graph

这样做的原因是 claim graph 体积远大于 page index，把它拆出去后，搜索、lint、普通 query 不必每次都读完整治理图。

## 测试

回归测试入口：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

当前覆盖重点包括：

- `query --dry-run` 不落盘
- `delete` 的安全语义
- review queue 并发写、去重、`item_id` resolve
- unified review 摘要输出
- multiline YAML source parsing
- 索引脏标记、损坏恢复、claim graph 拆分
- V8 migration / publish / debt / trace
- block-level claim extraction
- validity state inference

## 依赖

见 [requirements.txt](requirements.txt)：

- `filelock`
- `networkx`
- `python-dotenv`
- `python-louvain`
- `PyYAML`
- `watchdog`

## 版本状态

当前仓库的真实状态可以理解为：

- 运行时兼容层：V7.2 风格工作流仍保留
- 治理内核：V8 minimal runnable implementation

`gemini-extension.json` 里的版本号仍是旧扩展版本号，不应把它等同于当前内部架构成熟度。

## 设计原则

- 不引入外部数据库作为主存
- Markdown 继续承担人类可读、可审计的发布层职责
- canonical objects 才是身份、有效期和 provenance 的主权层
- 尽量兼容旧工作流，但不继续维持没有执行链路的历史壳
- 图谱、索引、队列都以“可恢复、可追溯、可并发安全”为优先目标
