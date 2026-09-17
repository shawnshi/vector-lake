# Unreleased

## 节点类型词表收敛为单一来源，并修掉它的副本已产生的 `System_` 缺陷

同一份词表在**五处**以手写形式存在，各不相同：

| 位置 | 形式 | 元素数 |
|---|---|---|
| `wiki_utils.py` `VALID_PREFIXES` | 前缀元组 | **11**（含 `System_`）|
| `tool_query.py` `_NODE_PREFIXES` | 前缀元组 | 10（**缺** `System_`）|
| `tool_query.py`（同文件另一处内联字面量）| 前缀元组 | 10（**缺** `System_`）|
| `wiki_utils.py` `validate_wiki_filename` 的正则交替 | 正则 | 10（**缺** `System`）|
| `schema_validator.py` `VALID_TYPES` | 小写类型集合 | 11 |

前两者的注释都写着“等同于 `wiki_utils.VALID_PREFIXES`”，但实际都不是。漏掉 `System_` 确实改变了行为，
但机制不是“链接匹配不到页面”：

- `_node_core()` 按前缀剥壳，而 `System_` 不在它遍历的列表里 → 其余 10 个前缀都会剥掉，只有它不剥；
- 存根创建处的内联字面量同样没有 `System_` → `System_*` 目标被标成 `type: concept`；
- 而 `schema_validator` 明确拒绝 `System_*.md` 文件名携带 `concept` 类型 → **那些目标因此从未生成存根**
  （写入被拒，只留下一条 `Failed to create stub` 警告）。

一旦把类型标对，写入就会**成功** —— 这正是本批额外增加一道拒绝的原因：`System_*` 是生成物命名空间
（活库有 **798** 个 `System_*` 页，全部由聚类守护进程产出，`indexer` 与页面投影都跳过它们）。为它写一个
存根只会让 lint 认为链接已解决、而图里永远不会出现该节点，把真实的缺口**藏起来**而不是报出来。因此
存根创建现在直接跳过生成物类型，让链接继续以“破链”形态暴露。

关于措辞的自我更正：初版把影响写成“指向已有 `System_Community_*` 页的链接匹配不到该页”。这不准确 ——
真正的原因是归一化不对称（`normalize_entity_name` 把前缀之后的 `_` 换成 `-`，而 `existing_cores` 的键
来自原始文件名），它对**所有**前缀一视同仁，不是 `System_` 特有的。`System_` 唯一独有的行为差异是上面
那条“类型标错 → 写入被拒”。

处置：新增叶片模块 `vector_lake/node_vocabulary.py`（**零 import**，因此任何层都能依赖而不成环、也不
拖入 wiki 文件系统），类型与上述四种派生物全部由同一个元组推导；`wiki_utils`、`schema_validator`、
`tool_query` 改为引用它。`tool_query` 的两份手写列表与内联字面量删除（后者抽成 `_node_type()`），
`_NODE_PREFIXES` 也一并删除（改为委派后它已无任何生产读者，只剩测试在读）。

保留不动：`tool_query.py` 的 `filename.startswith("Synthesis_")` 与 `tool_ingest.py` 的两处
`Synthesis_`/`Source_` 判断是真实特例（前者“Synthesis 页跳过质量门”，后者入库候选类型），不是词表
副本，合并它们才是混合模式。

### 可验证性

- **取值与顺序完全不变**：`wiki_utils.VALID_PREFIXES` 新旧逐元素相等；`VALID_TYPES` 新旧集合相等。
- 没有任何前缀是另一个前缀的前缀，所以 `strip_prefix`/`type_for_node_id` 的**迭代顺序不可观测**。
- 全仓搜索确认无任何地方对 `VALID_TYPES` 做原地修改，也没有 `isinstance(..., set)` —— 否则
  `set` → `frozenset` 会是破坏性变更（实测不是）。
- **正则改动与行为无关**：新旧正则只在 `System_` 上有差异（对活库 **7 923** 个文件名逐一比对，
  235 处差异**全部**是 `System_*`）。而 `validate_wiki_filename` 第一行就对该前缀 `return`，
  实测 `System_BAD NAME!!!.md`、`System_.md`、`System_` + 300 字符都能通过（而同样畸形的 `Concept_*`
  会被拒），证明那段正则对 `System_*` 不可达 → **函数行为未变**。
- 三者（现为两者）持有**同一个对象**，测试用 `is` 而非 `==`：一个还“碰巧相等”的副本也能通过 `==`。

### 守卫及其边界（诚实说明）

`tests/test_node_vocabulary.py` 新增 9 例，含一道源扫描守卫：`vector_lake/*.py` 不得出现 **≥3** 个
前缀字面量（重新声明一整个列表必然带 11 个）。当前 0 命中 —— `tool_ingest`/`tool_lint` 各有 2 个特例，
故用阈值而非全面禁止；另有一例专门验证该阈值真的会触发（不会失败的守卫不算守卫）。

它**拦不住**的：单引号字面量、f-string、用变量拼出的列表、手写正则交替、以及 `vector_lake/*.py`
之外的文件（例如 `templates/topology.html` 里的颜色表副本）。这些限制已写进测试 docstring。

独立复核还指出三处已修：守卫原用相对路径 `Path("vector_lake")`（换目录跑 pytest 会扫到 0 个文件却
静默通过），改为 `Path(__file__).resolve().parents[1]`；“模块无 import”测试原用正则只查 `vector_lake`
导入（`import os` 也能通过），改为 `ast` 断言不存在任何 Import 节点；测试 docstring 原称
`node_vocabulary` 自身不含前缀字面量，实际其 docstring 里有示例，已更正。

全量 pytest **727 passed**。

## 修复 `page_graph_edges` 与已发布边集不再一致（97.7% 的行是残留）

审计实测：`page_graph_edges` 有 **1 293 200** 行，而实际发布的 `index.json["weighted_edges"]`
只有 **29 837** 条 —— **1 263 363 行（97.7%）是发布集里不存在的**。原因不是逻辑错误，而是
“常量收紧后旧数据不会重算”：

- 全量重建路径用封顶后的集整体替换该表（`replace_page_graph_edges`），但增量路径
  （`replace_page_graph_edges_for_node`）**只重写本次改动涉及的节点**；
- 因此每个“此后再未被改动过”的节点都永久保留封顶前的行。活库最大节点度数为 **2 174**。

两张表本来就该一致：`page_index_edges` 是同一份 `weighted_edges` 的读取投影。于是这个契约
完全可以只在库内校验，不需要解析 `index.json`：

- `db_store.page_graph_edges_mirror_drift()`（只读）用两个 `LEFT JOIN ... LIMIT` 探针回答
  “是否已经偏离”，并用行数差给出规模与样例。刻意**不**用精确 `EXCEPT` 计数：实测在 1.29M 行时
  需要 **30.7 s**，对 `doctor` 来说不可接受；探针在同规模下是 **0.00 s / 0.16 s**。
- `doctor` 新增 `Page Edge Projection` 检查，偏离时计 FAIL + `page_edge_projection_drift` 告警，
  并直接给出一个不在发布集里的边样例。

**该检查不是度数上限检查。** 已发布的 `weighted_edges` 自身就有 **10 个节点度数为 16–17**，
超过 `MAX_EDGES_PER_NODE = 15`（`_apply_graph_topology` 并不加边，所以这与它无关），
即“≤15”从来不是这份产物真正满足的界限；真正满足的界限是“镜像已发布集”。

活库一次性修复用全量重建本来就调用的那句：
`db_store.replace_page_graph_edges(index.json["weighted_edges"])`。实测：

- 行数 1 293 200 → **29 837**，漂移 `(extra, missing) = (False, False)`；
- `PRAGMA quick_check = ok`；`doctor` → `[OK] Page Edge Projection: mirrors the published 29837 edge(s)`；
- 状态一致性 Wiki:7125 JSON:7125 SQLite:7125 不变，`page_index_edges` 与 `index.json` 未被改写；
- freelist 60 640 → **154 200 页（约 383 MB 可回收）**。**未执行 VACUUM**（2.13 GiB 的整库重写，
  工具与耗时均超出本批次范围），因此文件大小不变，空间在 freelist 中。

新增 `tests/test_page_edge_projection_mirror.py`（7 例）；全量 pytest **707 passed**。

### 本批**未**做的事，以及原因

审计建议里与本条同时提出的 “切断 `page_graph_edges` 投影回灌上游”（D1）**已实现又撤回**，
因为独立复核发现并实测确认它会静默丢边：

`update_index_items` 用**原始**链接字符串做预筛，而全量重建 `_calculate_weighted_edges` 先用
`alias_map` 把链接解析成 page key。因此“用别名写的链接”只在重建路径上成边，增量路径看不见它 ——
过去靠读回投影把这类边“托住”。实测已发布边集中：

- 按原始链接预筛，**116 条**边无法通过（会被丢）；
- 只修别名解析后仍剩 **63 条**（其余差异来自 `TYPE_AFFINITY` 不对称与共同邻居度数用的旧值）；
- 因此**无法用有限改动让增量路径等价于重建路径**，撤回是正确处置，而非绕过。

结论：只要读回仍在，该表就有读取者，不能删；而按上面的一次性重投影，读回变成“读回自己刚
发布的那 15 条以内边”，行为可证不变。D1 需要先把增量路径的推导对齐到重建路径（即 `update_index_items`
的预筛与 `calculate_relevance` 入参都要先过别名解析，且以 `min`/`max` 规范方向取 affinity），
否则不应改；这次尝试的完整改动已撤回，未提交。
## 删除树内无法创建也无法清理的 schema 残留

2026-09-16 的 `526df6a`（"Local tree becomes the authoritative main line"）删除了 `db_store.py` +
`governance_store.py` 共 23 856 行和 `tests/test_retention_v6.py`（2 232 行），其中包括整套 v6
retention 机制。但被删代码已经用 `CREATE ... IF NOT EXISTS` 把对象写进了活库，而没人发过一条
`DROP` —— 于是这些对象在库里存续至今，树内既看不见也清不掉：

- `ingest_jobs` 表 + `idx_ingest_jobs_job_id` + `idx_ingest_jobs_status`：全仓**零条 SQL 引用**该表。
  `ingest_jobs` 这个词只出现在 `requeue_legacy_ingest_jobs` 这个函数名里，而该函数查的是 `jobs`。
- `idx_jobs_retention_v6` / `idx_mutation_outbox_retention_v6`：为已经不存在的 retention DELETE 建的
  索引。**清理逻辑消失而索引留下**，这就是 `mutation_outbox`（32 280 行全终态，最老 2026-07-12）与
  `jobs` 此后无界增长的直接原因。
- `idx_mutation_outbox_idempotency_lookup`：`_ensure_idempotency_index` 只创建 `{name}` 与
  `{name}_active`，从不创建 `_lookup`。
- `claim_graph_nodes` 表：0 行。树内创建它、在 canonical 级联里 DELETE 它，但从不 INSERT、从不读取。
- `change_sets.change_id` 列：26 774 行全为 NULL，且字符串 `change_id` 在仓库任何 Python 文件中
  0 次出现。

处置：新增一次性 prune 机制（`db_store._LEGACY_SCHEMA_PRUNES` + `schema_migrations` 台账）。每条迁移
是一个名字 + 有序步骤，步骤是**可调用对象**而非 SQL 字符串，因而可以自己做判断（探测列是否存在），
而不是靠异常文案区分「已完成」与「失败」。

- prune 先只读台账，已收敛的库只发生一次 SELECT、**完全不取写锁**；只有存在待处理迁移时才开事务。
- 每条迁移自持事务，前面的成功不会被后面的失败回滚。
- 迁移只在全部步骤成功后记账，失败则保持待处理、下次重试。
- prune **不是** `_schema_is_complete` 的条件。曾经把它写成条件，结果会让「prune 之前做的只读快照」
  上 `init_db()` 直接失败（而它缺的只是一次清理）；现在由 `init_db()` 两个分支末尾的
  `_apply_prunes_best_effort()` 承担，失败只告警并把 schema 标为未收敛（`doctor` 会报 FAIL），
  库仍可用于读取。
- 表缺失通过 `sqlite_master` 探测，而不是 catch 后返回空集：把任何瞬时读失败当成「还没应用过」
  会导致重跑已完成迁移并**覆盖 `applied_at`**，台账就不再是「迁移实际何时执行」的记录。

**独立复核（builtin `reviewer`，未参与生成）发现并已修复一个真缺陷**：原先靠
`"no such column" in message` 容忍失败。实测 SQLite 对「列被索引引用而无法删除」报的是
`error in index ... after drop column: no such column: ...`——**包含**该子串，于是真实失败被当作已完成，
列还在而台账声称已删。改为 `PRAGMA table_info` 探测后再决定是否发 ALTER，不留任何容忍项。
同时按复核意见收紧了三点：每条迁移独立事务、失败不得被读错误伪装成「空台账」、
`applied_schema_prunes()` 不再调用 `init_db()`（诊断不应写库）。

- 与 prune 同时删除的死代码：`db_store.page_graph_degree_map`（全仓 0 调用点，且是
  `page_graph_edges` 1 293 200 行的唯一全表读取者）。
- `governance_store.ALLOWED_TABLES` 移除 `claim_graph_nodes`。
- `doctor` 新增 `Schema Migrations` 检查（未收敛时计 FAIL + `schema_prune_pending:` 告警）。

验证（活库 `C:/Users/shich/MEMORY`，2.29 GB）：对象数 130 → 123；`PRAGMA quick_check` = `ok`；
`change_sets` 26 774 行无损；`operational_memory` 146 679 行无损；
`State Consistency: Wiki:7125 JSON:7125 SQLite:7125`、`Write Gate: clean`、
`Idempotency Index: jobs=full(dups=0), mutation_outbox=full(dups=0)` 全部保持。
**回滚演练**：按记录的恢复点重建全部 7 个对象 + `change_id` 列（实测全部成功恢复），清空台账后重跑
`doctor`，7 个对象再次全部消失、台账再次记满 2 条 —— 恢复路径与再收敛都已实测，不只是文档。
新增 `tests/test_legacy_schema_prune.py`（11 例，含复核 P1 的索引列回归与「读失败不得伪装成空台账」）；
全量 pytest **700 passed**。

## 删除一个对无界边投影的全表读取函数

`db_store.page_graph_degree_map` 在全仓（runtime / tests / docs / skills / templates）0 调用点，
其 docstring 自己已指向别处（"Orphan detection must use canonical topology"），即被取代后没有被删。
它是唯一会全表扫描 `page_graph_edges`（1 293 200 行）的函数；删除后该表只剩 1 个读者
（`indexer.py:1206` 的增量回读），也就是让它能回灌自己上游的那条读写闭环。
全量 pytest 689 passed（无变化）。

## 聚类守护进程两个致命阻塞修复（修复前该脚本无法在活库上跑完一轮）

`scripts/community_clustering_daemon.py` 不被 watchdog 调度（`CONTEXT.md:84` 明确写 "not scheduled"），因此它的批量路径长期无人执行、从未暴露过下面两个缺陷。它们各自都能让整轮运行在写入前一条边也不落盘的情况下终止。

- **YAML 标量未转义（阻断一）**：社区索引页的 frontmatter 由 f-string 手写 `title: "{label}"`，而 label 由两个成员标题拼接，标题里只要有一个反斜杠就炸：活库 7,123 个标题中恰好有一个（`paper-\Delta-mem`，LaTeX 片段），生成页在 `load_yaml` 抛 `ScannerError: found unknown escape character`。由于 `_prepare_mutations` 在提交前校验整批，**单个标题就终止了整轮运行**（实测：21:11:24 启动，21:16:38 失败，exit 1；仓库未发生任何变更——index.json 哈希与备份一致、577 个社区页原封不动）。新增 `_yaml_scalar()` 用 `json.dumps` 输出合法的 YAML 双引号标量（YAML 1.2 是 JSON 超集，反斜杠与引号均被转义），title 与 aliases 均改用它。验证：反斜杠 / 双引号 / 中文三类 label 均能 frontmatter 往返完全一致并通过 `schema_validator`。
- **SQL 变量上限（阻断二）**：`_apply_change_sets_batch_unchecked` 用 `IN (?,?,...)` 按 id 展开占位符，而 SQLite 上限为 32,766（本机 3.50.4 实测），`claim_graph_edges` 那条语句又把每个 id 绑定两次；批量重写社区索引页触及的 claim id 达 14,055 个（`claims` 表中 `locator.page_key LIKE 'System_Community%'` 的实数），远超 16,383 的半数上限，抛 `sqlite3.OperationalError: too many SQL variables` 并因整批同属一个事务而全部回滚（实测第二轮：21:18:30 启动，21:20:02 失败）。新增 `_json_id_list()`，把 4 条语句改为 `IN (SELECT value FROM json_each(?))`：无论 id 多少都只占 1 个参数，并保持 `sorted()` 的确定性参数序。验证：17,000 个 id 下旧写法复现 `too many SQL variables`、新写法通过（参数 2 个）；新增 `tests/test_canonical_change_sets.py::test_page_rewrite_survives_claim_ids_beyond_the_sql_variable_ceiling`（预置 17,000 条 claim 后重写页面，断言无异常且旧 claim 被退休）。
- **运行前置：先让边集收敛**。首轮失败时 `weighted_edges` 仍是修复前的 1,607,841 条，守护进程的 `nx.Graph` + `nx.pagerank` 在 160 万条边上耗时约 5 分钟（失败耗时主要是这一段）。先执行一次增量更新（`indexer.update_index_items(["Vendor_Google.md"])`，16 秒）把活库边集按新的共享剪枝收敛到 29,815 条（最大度 15、0 重复），守护进程随即只需 2 分 04 秒完成。
- 验证（活库，已授权执行）：exit 0，`V9 Heavy graph topology clustering complete.`。社区索引页 577 → 568（537 个原址重写、31 个新建、40 个退休、5 个手写命名页全部保留）；`communities` 7,178（含 65 个陈旧键）→ 7,123（恰好等于节点数）、897 个稳定 ID、0 冲突；`community_labels` 0 → 265（每个 label 都有对应的 L0 页面）；`graph_insights` 0 → 20（全为 `isolated_node`，与稀疏社区在活图上不触发一致）；`graph_state.clustering_stale` → false。消费端实测复活：`tool_research.research_vector_lake(dry_run=True)` 由原先永远返回 “No research required” 变为产出 15 条研究指令，其中包含由 `isolated_node` 派生的图谱缺口查询。回滚点：`MEMORY/backups/graph-precluster-20260917-211106/`（index.json 166 MB + 577 个社区页 + community_snapshot.json + 聚类前状态四元的 JSON 快照）。
- 残留：写入 `index.json` 用的是 `atomic_write_text(..., indent=2)`，绕过了 `_write_index`，因此页面索引投影会短暂落后（运行中出现 `projection_drift:missing_index=1`，下一次 `_write_index` 已自愈）；守护进程全程持锁，运行期间出现 `db_write_lock_contention:timed-out@99s`；运行中的 watchdog 进程仍持有旧代码，本轮结束后它写入的 `graph_state` 只有 `dirty:true`（未设置 `clustering_stale`），需重启该进程后新旧语义才一致。

## 图谱输出路径默认改为 `<MEMORY>/scratch`

- `tool_graph._graph_output_path` 的首选位置原为 `<memory_dir>` 的**上一级** `tmp/`，即不传 `output_dir` 时会把生成的仪表盘写进宿主家目录布局（备选是 `<extension_root>/data/tmp`），与 `skills/graph` 自述的“禁止向全局路径盲写”相矛盾。现改为 `<memory_dir>/scratch/vector_lake_graph.html`，与技能对显式 `output_dir` 要求的隔离区一致；extension-root 备选仅保留为 scratch 不可写时的降级路径。
- `skills/graph/SKILL.md` 同步：`output_dir` 改为可选（默认即 `<MEMORY>/scratch`），阻断点改为针对“显式传入且超出获批沙盒”的情况；技能版本 11.1.0 → 11.1.1。
- 验证：新增 `tests/test_graph_algo_contract.py::test_default_output_path_is_the_memory_scratch_tree` 与 `::test_default_output_path_never_escapes_the_memory_root`（断言产物落在 memory 根之内、不再落在其上一级）；活图无参调用 `visualize_vector_lake()` 实测返回 `C:\Users\shich\MEMORY\scratch\vector_lake_graph.html`（10,868,401 字节，重写），`C:/Users/shich/tmp` 与 `vector-lake/data/tmp` 均未创建。

## 图谱可视化四层算法的六个不变量修复（P0-P2）

活图实测的六个断裂点：边集违反自身剪枝上限、社区视图全量渲染失效、社区 ID 稳定化把划分退化成合并、斥力只覆盖 45.8% 节点、连通保底承诺不成立、载荷把节点数组序列化两遍。逐项修复如下。

- **边集不变量下沉为共享函数（P0）**：新增 `indexer.dedupe_and_prune_edges(edges, max_edges_per_node=MAX_EDGES_PER_NODE)`，同时保证“每个无向对只出现一次（min/max 归一）”与“没有节点超过 15 条边”。此前两条规则只在全量重建路径生效，`update_index_items` 的增量路径对触及节点追加全部合格边、又从 `page_graph_edges` 投影回捞一次、且从不重剪——活图因此涨到 1,607,843 条边 / 7,125 节点（平均度 487.7，最大 4,118，314,660 条重复对），而它自己的 SQLite 投影（主键 `source_id,target_id,relation`）只有 1,293,183 行，**索引文件与其投影互相矛盾**。修复后同一份活数据：29,815 条边、最大度 15、0 超限、0 重复，且对逆序输入结果逐字一致、对已剪结果幂等。
- **社区视图恢复（P0）**：`communities` 的值是 8 位 hex 字符串，前端 `COMMUNITY_COLORS[cid % length]` 实算 `'6fd2edf4' % 10 === NaN`，`COMMUNITY_COLORS[NaN]` 为 `undefined`，`ctx.fillStyle = undefined` 被 Canvas 静默忽略——实测 2,000/2,000 节点颜色索引无效，社区模式整图同色。改为 `communityColorIndex(token)`（字符串稳定哈希）并对未分配节点使用独立灰色。活图 7,111 个带社区的节点全部得到合法索引并分布在 10 个色桶（499/746/1227/1164/985/311/678/479/530/492）。
- **社区 ID 稳定化不再合并簇（P0）**：`_stabilize_community_ids` 改为按重叠量降序认领、旧 UUID 一旦被认领即排除，认领冲突时铸新 ID（并避开全部保留 ID）。此前只取“最大重叠旧 UUID”而不标记已占用，合成用例 5 个新社区被压成 3 个 ID，输出不再是划分，`process_level` 还会把两簇写进同一个 `System_Community_<level>_<id>.md`，后者覆盖前者。活图 L0：897 个 Leiden 社区 → 897 个稳定 ID、0 冲突；L1：930 → 930、0 冲突；用自身快照重跑两轮结果逐字相同（这是社区索引页不被孤立的前提）。另在 `process_level` 加入同一文件名二次写入的拒绝守卫，把静默覆盖变成一条 error 日志。
- **斥力改为空间哈希（P1）**：原实现外层步长 `max(1, N/200)`、内层只扫 `[i+1, i+16)`，在 7,125 节点下实测 3,264/7,125（45.8%）节点参与过斥力，**3,859 个节点全程零斥力**（只能被链路吸引拖走，必然压成同心重叠），且是否参与取决于数组下标。改为按斥力半径（100px）分桶、每个节点只访问 4 个半平面邻格，覆盖 100.0% 节点。JS 实测（node 25，同规模、同螺旋初值）：2.20 ms/迭代、63,508 次配对评估，旧方案 0.06 ms/迭代、3,060 次评估；同时给 `alpha` 加衰减（起点 0.3、每步 0.985、下界 0.001）——此前 alpha 恒为 0.08 且 `isSimulating` 永不复位，布局永不收敛。
- **连通保底改为覆盖优先（P1）**：`_extract_backbone_edges` 由“权重降序 + 有未覆盖端点即选”改为“配对覆盖 → 补齐孤立节点 → 按权重填满配额”，并先按无向对去重（重复行不得消耗配额）。原实现在 5,000 边预算下用 5,000 条边只覆盖 6,216 个节点、留下 909 个孤儿，`max backbone degree` 达 232——所谓骨架其实是毛刺；docstring 声称“保证每个连通节点保留其最强边”与行为不符，已改为准确表述（保证被表示，不保证是它最强的那条）。修复后同一数据：覆盖 6,594 个节点、孤儿 529，而这 529 恰好是**真实无边孤立节点数**（529），最大 backbone 度降到 21。
- **载荷去重与真度（P1/P2）**：`_build_graph_payload` 不再在顶层重复 `nodes`/`edges`/`community_labels`（此前与 `pageGraph` 同内容，实测 HTML 里 `"node_kind": "page"` 出现 14,248 次 = 2 × 7,124，把含页面摘要的节点对象写了两遍）。实测 HTML 由 15,217,470 字符 / 19,377,941 字节降到 8,717,708 字符 / 10,868,401 字节（-43%），页节点对象恰好序列化一次。
- **`degree` 改为真实无向度（P2）**：原实现按 `links` 逐条自增（含悬空与未解析），入边仅在别名解析成功时计入，实测 6,365/7,123 节点（89%）与真实度数不符，最极端的 `Institution_浙大一院` 报 3 而实为 791。改为从去重后的 `weighted_edges` 计算，实测不一致 0/7,123（同一节点现在报 775）。
- **死管道复活（P2）**：`graph_insights` 自 Louvain 迁移后只有初始化、没有生产者，`tool_graph.audit_graph` 永远回答 “No graph insights found”，`tool_research` 的图谱缺口扫描永远空转；`community_labels` 同样只被赋 `{}`，UI 因此显示裸 hex。daemon 现按消费者契约产出 `isolated_node` / `sparse_community`（各限 20 条，避免淹没治理队列），并写入 L0 标签。同时修正 `audit_graph` 的队列标题——原标题不含节点名，按标题去重会把同类型的所有洞察压成一条。
- **脏标记与陈旧度可见（P2）**：`_apply_graph_topology` 原本把 `dirty` 又置回 `True`，导致该标记永不清除、`refresh_graph_topology_if_dirty` 每次都报“有变更”，且 `centrality_score` 只写过占位值（实测 4,645 节点为 1.0、2,478 节点无此键）。现拆分为 `dirty`（边拓扑，由本模块清除）与 `clustering_stale`（社区划分，由 daemon 清除，跳过聚类时保持为真），旧载荷无该键时回退到 `dirty`。`tool_graph` 在 meta 中新增 `communities_stale` / `community_labels_available` / `read_consistency` / `orphan_page_nodes`，模板新增陈旧告警徽标，工具返回值在社区过期或降级读时显式告警。
- **锁超时与降级显性化（P2）**：`visualize_vector_lake` 对 169 MB 的 `index.json` 只等 5 秒锁，实测**每次调用都超时**并静默落入无锁读；改为 20 秒（可传参），并在载荷 `meta.read_consistency` 与返回串中标记降级。修复后同一活数据在锁内完成（`read_consistency: locked`）。
- **前端杂项（P2）**：`PALETTE` 补 `Institution` / `Policy` / `Standard` / `Claim` / `System`（前三者是 `VALID_PREFIXES` 中的一等类型，实测 187/7,123 节点此前落到灰色 Unknown）；`onlyRisk` 改走共享 `isRiskyNode()`——原判据 `alignment_score >= 80` 因实测全部为 100.0 而恒真、且把大写 `status === 'Contested'` 与小写字面量比较、又对无该字段的 claim 节点求值，四种情形下都不可能过滤；删除从未被读取的 `timeFilterRatio` 死变量。
- 验证：新增 `tests/test_graph_algo_contract.py`（18 例，覆盖去重/封顶/确定性/幂等、骨架覆盖率与关系分级、社区 ID 唯一性与稳定性、载荷去重与真度、陈旧度回退、洞察契约与上限）；全量 pytest 676 例通过；另有 4 例失败属于本次改动之外的既有问题——`test_ingest_contract.py` 两例源于工作区未提交的 `native_llm._stable_task_root()` 把 ingest 包移到 `MEMORY/wiki/.meta/subagent_tasks/` 后触发 `remove_task_packet` 的 brain-tree 守卫，`test_runtime_health.py` 一例源于未提交的 `runtime_health.py` 把 `mutation_outbox_failed` 从 `issues` 降为条件门控，`test_portability.py[ingest_runner.py]` 源于本次会话期间新出现的未跟踪文件 `scripts/ingest_runner.py` 内硬编码 `C:/Users/shich/MEMORY`；活图验证使用 `MEMORY/scratch/accept_graph_fixes.py`（只读，7,123 节点重放全部指标）与 `MEMORY/scratch/measure_repulsion.js`（node 25 实测新旧斥力开销），`node --check` 校验生成 HTML 的内联脚本语法通过。
- 未验证：未对活 `MEMORY` 运行聚类 daemon（其写入未被本次授权覆盖），因此活图 `community_labels` 仍为空、`clustering_stale` 仍为真——工具现在会显式告警而非静默展示过期划分；`weighted_edges` 的收敛要在下一次批次更新或全量重建时才落到磁盘。

## lint 自动合并改走共享合并器 + 治理项与其效果同事务提交

- `tool_lint` 的相似度自动合并分支改为调用 `semantic_merge.merge_markdown_content`，并复用 `governance_metrics.establishment_key` 的「最老者幸存」方向规则。此前它自做朴素拼接（同一个 schema 缺陷，被吞并页的编译事实条目会落到 `## 2. 证据时间线` 之后），且方向按 `updated` **最新者**保留——与生成器规则相反，同一重复对在两条路径上会朝相反方向合并。**该分支当前是死代码**（外层 `if False: # auto_fix disabled for similarity merge by Mentat`），本次只保证一旦重新打开行为正确；是否直接删除该分支另议。
- `governance_service.resolve_governance_item` 把队列锁提到数据库事务之前（维持 file-lock → DB transaction 的锁序），并将队列项状态更新放进 mutation 的 `canonical_callback`，与 canonical 变更同事务提交。此前两者分属不同事务：客户端或进程在 canonical 已提交、队列项未标记的窗口内死亡，会留下「已合并但仍 pending」的条目，而副页已删，重跑永远无法成功。
- 验证：`tests/test_merge_resolution_guard.py` 新增两例——队列写入必须发生在 mutation 事务内；提交回调内部抛错时正文、副页与队列项三者一并回滚。
- 数据修复（非代码）：活图 `alias_registry` 中 `Google Cloud -> entity_90ce1b512589e8544687e3b6` 一行丢失，已用 `governance_store.upsert_alias` 按该实体自身的 `canonical_name` 定向恢复。**删除原因未查明**：已排除 `save_alias_registry`/`rebuild_alias_registry`（无调用方）、当日针对该页的 change set（无）、以及合并路径（隔离环境复现保留该行）；曾提假设“受影响页提取为空时只剩别名行被清”——被 `_apply_change_sets_batch_unchecked` 的 page-scoped `DELETE FROM entities` 证伪（该路径连实体行一起删，无可恢复对象），相应改动已撤回。

## 合并器分节处理 + 歧义名判定下沉到解析入口

- `semantic_merge.merge_markdown_content` 由「整体拼接右页正文」改为**分节合并**：两侧正文被分配进同一个 Section 1 与同一个 Section 2，H3 区块按标题合并去重，时间线条目并集去重。旧拼接必然触发 `schema_validator` 的事件账本校验——它把 `## 2. 证据时间线` 一直扫到文件末尾，于是被吞并页的编译事实条目被读成非法时间线条目。活图 361 对真实页面实测：旧拼接 209 对不合法，新合并 0 对不合法。
- Section 1 的 H3 白名单按**幸存页**类型收敛：对幸存页类型非法的槽位降级为粗体标签而不丢事实。`TENSION_H3_SLOT` 改为随 `tension_edges` 条件加入，并提为 `schema_validator.TENSION_H3_SLOT` 单一常量由校验器与合并器共用，避免字面量漂移。这一条来自实测：首版分节合并把携带 `tension_edges` 的页面弄坏——它降级了校验器仍然索要的那个槽位。
- 歧义名守卫下沉到 `governance_service.resolve_governance_item` 入口：合并前按两侧 page key 计算 `ambiguous_name_hazards`，命中即拒绝且保持队列项 `pending`；`change_manifest: {"allow_ambiguous_names": true}` 显式放行。此前守卫只在生成器入队路径生效，`bulk_reconciliation` 自建 `merge_candidate`、从不调用生成器，可直接绕过。
- `governance_metrics` 抽出 `_names_of` / `_name_owner_index` / `ambiguous_name_hazards`，生成器与解析入口共用同一判定。
- 验证：全量 pytest 618 例通过；`MEMORY/scratch/accept-merger.py`（361 对真实页面，只读，旧 209 不合法 → 新 0 不合法）；`MEMORY/scratch/accept-generator.py`（生成器三项指标）。

## 合并候选生成器的三处契约缺陷修复

- `left_name` / `right_name` 改为输出磁盘上的 page key。此前输出 `canonical_name`，而 `resolve_governance_item` 按 `<prefix><name>.md` / `<name>.md` 解析页面；活图实测 361 条候选中 266 条（441 个名称位）解析不到任何文件，等于永远无法合并。原始标题保留在新字段 `left_canonical_name` / `right_canonical_name` 供展示。实测 441 → 0。
- 幸存方改为确定性选择：`created_at` 更早的一方保留（缺失者排最后，再以 `entity_id` 兜底）。此前方向来自候选配对的集合遍历顺序，即随机；现已写入 `reasons`（`direction:older-entity-survives`）可审计。两次独立运行结果逐对一致，违反“最老者幸存”的候选为 0。
- 新增 `hazards`：共享名被 ≥3 个不同实体声明时不入队。此类配对无法由检测器决定保留哪个节点（活图实测 9 条，涉及「四层壳模型」「Agent Skill 标准结构」「电子病历系统功能应用水平分级评价」）。候选项仍出现在预览面与 `find_merge_candidates` 中，可用 `create_merge_suggestions(..., include_hazardous=True)` 强制入队。
- 明确未采用「名称超集」启发式：探针显示它会把 `AI Factory`、`Intelligence Hub Briefing [2026-08-09]` 等大量真实重复误判（`ai`、`memory`、`factory` 这类单词名污染了名称空间），故不落地。
- `merge-suggestions` 预览新增 `skipped_hazardous` 计数与逐条 `HAZARDS:` 标注。
- 验证：新增 `tests/test_merge_candidate_contract.py`（4 例）；全量 pytest 610 例通过。

## MCP 服务端依赖换为 `fastmcp>=4.0.0`

`vector_lake/mcp_server.py` 直接导入 `fastmcp.FastMCP`，移除 `mcp.server.MCPServer` / `mcp.server.fastmcp` 双路径回退。

- 同步工具清单兜底改写 fastmcp 4 的 `local_provider._components`：`_tool_manager` 在 4.x 已不存在，旧兜底会静默返回空列表；`list_tools()` 仍只有协程形态，原有“无事件循环时 `asyncio.run`”的路径不变。
- `requirements.txt`：`mcp>=2.1.0` → `fastmcp>=4.0.0`；`requirements.lock.txt` 改钉 `fastmcp==4.0.4`。MCP Python SDK（`mcp` 2.2.0）由 `fastmcp-slim[client,server]` 传递解析，不再作为直接依赖声明。
- 传递依赖净增 27 个发行包（fastmcp-slim、cyclopts、griffelib、joserfc、authlib、keyring、py-key-value-aio 等）。解析后逐版本查 OSV 均为 0 记录；fastmcp 许可 Apache-2.0，仓库 PrefectHQ/fastmcp。
- 启动期外部调用归零：fastmcp 默认会在启动时请求 pypi.org 查版本并打印 banner，现固定 `FASTMCP_CHECK_FOR_UPDATES=off` 且 `mcp.run(show_banner=False)`——stdio 通道的 stdout 承载 JSON-RPC，不能有横幅噪声。
- 验证：`registered_tool_names()` 仍返回 36 个工具；`python -m vector_lake.mcp_server` 经 stdio 实测握手列出 36 个工具并成功执行 `get_governance_debt`；全量 pytest 通过。

# Vector Lake 11.20.2

## 删除丢失子系统的残留数据 + VACUUM（操作方选项 A）

### 已验证备份

三个恢复点，**均以只读方式打开核验**（`file:...?mode=ro` + `integrity_check` + 回读行数），并核查了已删集的每一行：

| 备份 | 大小 | 表数 | 内容 |
|---|---|---|---|
| `backups/vector_lake_1789575103.db.bak` | 3 622 MB | 81 | 删失效检索投影之前（尚无 gram/页投影，符合预期） |
| `backups/vector_lake_1789596868.db.bak` | 3 887 MB | 73 | generation 机制清理之前（gram 414 914 / 页节点 7 178 已存在） |
| `backups/vector_lake_1789597629.db.bak` | 3 451 MB | 58 | **丢失子系统数据清理之前，18 张表的 1 346 635 行全部可恢复** |

三者 `integrity_check` 均为 `ok`，且都包含 `operational_memory 142 117 / claims 120 339 / evidence 139 339 / entities 8 489 / timeline_events 9 286`。

> 核查过程中一次 `ls | head` 与一次 `os.listdir` 都返回空目录，触发了误报的数据丢失警报；改用 `ls -1 | wc -l`、`du`、`find` 以及逐个打开文件后确认备份始终存在。**空列表不能作为不存在的证据** —— 上表的只读打开才是正向核验。

### 删除的对象

**18 张表 / 1 346 635 行**，创建方与读取方均随丢失版本一起消失：`evidence_versions`(494 879)、`claim_versions`(432 439)、`canonical_identities`(302 705)、`wiki_edges`(29 706)、`extraction_runs`(24 181)、`change_set_lifecycle_v6`(22 883)、`entity_identities`(12 425)、`wiki_nodes`(9 003)、`embedding_metadata_v8`(7 169)、`ingest_stage_events`(4 370)、`source_artifacts`(3 210)、`ingest_task_cleanup`(2 537)、`merge_journal`(489)、`history_retention_runs_v6`(303)、`ingest_outbox_links`(282)、`claim_assessments`(43)、`schema_migrations`(9)、`api_users`(2)，以及**仅服务于这些表的 4 个守卫触发器**。

表 73→40、触发器 65→16。**所有保留表行数逐一比对未变**，`integrity_check` ok，`foreign_key_check` 干净。删除前对每个对象断言：代码无引用、无活动触发器/视图交叉引用。

### 一次差点出事的拦截

首次枚举把 `vec_embeddings_chunks` / `_info` / `_rowids` / `_vector_chunks00` 也列为候选 —— 名字过滤器排除了 FTS 影子后缀（`_data`/`_idx`/`_docsize`/`_config`/`_content`），却没排除 sqlite-vec 的。这四个是**活动虚拟表 `vec_embeddings` 的影子表**，删掉会直接摧毁向量投影（7 169 条 embedding）并把混合检索静默降级为纯词法。过滤器已改为排除所有活动虚拟表的影子命名空间，`vec_embeddings*` 与 `wiki_search_index*` 完好且已按行数验证。

### `VACUUM`

两轮：**3 887 MB → 3 451 MB**（freelist 353 MB → 0，68 s）、**3 451 MB → 1 524 MB**（freelist 1 928 MB → 0，42 s）。每轮后 `integrity_check` ok、`foreign_key_check` 干净、行数未变。

整个会话下来存储 **3 622 MB → 1 524 MB（−58%）**，而且是*新增*了 n-gram 索引（97 MB postings）与页投影（~12 MB）之后。

### 验证

- `pytest tests -q` → **487 passed**；`ruff` 错误数与改动前一致（65 条全为既有）。
- 线上不变量：gram 索引 ready/usable、页投影 7178/30140、memory 投影 142117/142117 drift 0/0、timeline parity 9286/9286；重新执行 `init_db` 后表/触发器仍为 40/16（确认不会重建已删对象）。
- 清理后读路径：`assemble_context` 1.38 s、`search[page]` 0.215 s、`search[memory]` 0.486 s、`operational_memory_search` 0.427 s、`timeline` 0.057 s，与清理前同噪声水平。

# Vector Lake 11.20.1

## 数据库维护：删除无创建方无读取方的对象 + VACUUM

### 已验证备份

- `backups/vector_lake_1789575103.db.bak`（3 622 MB）——删失效检索投影之前，`integrity_check ok`
- `backups/vector_lake_1789596868.db.bak`（3 887 MB）——generation 机制清理与 `VACUUM` 之前，`integrity_check ok`，并从备份回读行数

### 删除的对象

在删除前对**代码文件**（.py/.sql/.toml/.json/.cfg/.ini，143 个文件）逐一做名字扫描，并对线上 `sqlite_master` 做触发器/视图交叉引用，确认无创建方、无读取方、无任何活动对象引用：

- **45 个** `trg_*_generation_v3_*` 触发器（15 张表 × insert/update/delete）——它们只 `UPDATE runtime_generations`，而后者无人读取；
- `runtime_generations`（15 行）与 `projection_runtime_v9`（1 行）——后者没有任何触发器、也没有任何代码引用；
- **13 张空表**：`api_rate_limits`、`change_set_payload_refs`、`change_set_payloads`、`critical_decision_registry`、`embedding_jobs`、`embedding_rate_events`、`entity_redirects`、`lost_and_found`、`mutation_intents`、`projection_outbox`、`quality_evaluation_runs`、`schema_registry`、`wiki_embeddings`（均 0 行，删除零数据损失）。

表 73→58、触发器 65→20。**所有保留表的行数逐一比对未变**，`integrity_check` ok，`foreign_key_check` 干净。

删除脚本对每个对象断言前置条件（代码无引用、表为空、无活动触发器引用），不满足即中止；文档（含本次报告）提及不算创建方或读取方。

**保留的 20 个触发器**：10 个 `required_columns_v1` 完整性守卫、4 个旧版遗留守卫（`canonical_identities` 的 append-only/owner-conflict、change-set 终态不可变），以及本次新增的 6 个。守卫**故意保留**——它们在执行契约，而不是无人读取的记账；删掉会静默削弱写路径。

### `VACUUM`

**3 887 MB → 3 451 MB（−436 MB）**，freelist 353 MB → 0，`integrity_check` ok，`foreign_key_check` 干净，行数全部未变，耗时 68 s。

### 未删除：丢失子系统的数据（需单独决策）

代码侧名字扫描同时发现 **约 131 万行、25 张表现在既无创建方也无读取方** —— 属于那个模块已丢失的版本：`evidence_versions`(494 879)、`claim_versions`(432 439)、`canonical_identities`(302 705)、`wiki_edges`(29 706)、`extraction_runs`(24 181)、`change_set_lifecycle_v6`(22 883)、`change_set_idempotency`(21 992)、`entity_identities`(12 425)、`wiki_nodes`(9 003)、`embedding_metadata_v8`(7 169)、`ingest_stage_events`(4 370)、`source_artifacts`(3 210)、`ingest_task_cleanup`(2 537)、`merge_journal`(489)、`history_retention_runs_v6`(303)、`ingest_outbox_links`(282)、`claim_assessments`(43)、`schema_migrations`(9)、`api_users`(2) 等。

这些是**历史与审计制品，不是记账噪音**：`claim_versions`/`evidence_versions` 是追加型版本账本，`canonical_identities` 是身份/别名账本，`wiki_nodes`/`wiki_edges` 是更早的页投影。它们当前无人读取，但也是那个丢失版本留下的**唯一记录**，且受 `append_only` 守卫保护；`ARCH_2026-09-17.md`（操作方自己的评审）把丢子系统列为项目最大架构风险且“没有干净解法”。基于名字扫描启发式销毁 131 万行属于**目标决策而非维护步骤**，因此**未执行**，需要按组明确 go/no-go。

### 附带修正

`compact_memory_gram_overlay()` 现在接受 `limit_grams=None` 表示整批清空（分块进行，上限 `COMPACT_MAX_GRAMS`），修掉了 MCP `compact_memory_gram_index` 调用会 `TypeError` 的缺陷。

### 验证

- `pytest tests -q` → **487 passed**；`ruff` 无新增问题。
- 维护后重跑基准：`assemble_context` 1.128 s、`search[page]` 0.192 s、`search[memory]` 0.468 s、`timeline` 0.056 s、`operational_memory_search` 0.424 s。
- 线上不变量：gram 索引 ready/usable、page 投影 7178/30140、memory 投影 142117/142117 drift 0/0、timeline parity 9286/9286。
- 入库写路径（40 行的页）：写入 **1.1–1.4 ms/页**；下一次读取的自愈（物化 + 有界压缩）**12–18 ms/页**。

# Vector Lake 11.20.0

## 三项后续：ngram 倒排索引、删除失效投影、index.json 投影为邻接表

详细测量与证据见 `PERF_2026-09-16.md`。三项均先打样测量再实现，不是拍脑袋选的方案。

### 1. `memory_gram_index`：精确 n-gram 倒排索引

剩余最差路径是 `_query_terms` 把一个中文查询展开成**每个单字 + 每个相邻 bigram**，使 SQL 打分变成 `O(行数 × 词项数)`。先做了两个打样：

| 方案 | 长查询延迟 | 排序 |
|---|---|---|
| FTS5 + bm25（列权重 4/3/1/1） | 240–300 ms | **改变**：旧 top-12 召回仅 3/12–12/12 |
| 精确 n-gram postings | 0.10–0.58 s | **与全表扫描完全一致** |

FTS5 方案是因为**改变排序**被否决，不是因为慢。n-gram 方案实测最长现实查询只触及 **223 253 条 posting**，纯 Python 累加 0.12 s——不需要 numpy，也不需要自定义压缩依赖。97 MB postings 取代了本来需要 ~450 MB 的 25.4 M 行 SQL 记录。

结构：`operational_memory_gram(gram, postings)` 存打包的 little-endian `uint32` 数组（`(doc_delta << 4) | field_mask`）；`operational_memory_gram_overlay` 用普通 B-tree 行承载新增写入，使单个文档的更新是 `DELETE WHERE doc = ?` 加一次插入；`operational_memory_gram_dirty` 标记“基础 postings 不可信”的文档，**跳过**它们的 base 条目——这正是"不存旧状态也能精确合并"的关键，同时也自然退役了被删除的文档。

线上验证：**11 组查询 × 4 组过滤组合全部与 `legacy` 全表扫描逐条 id 一致**，0.098–0.583 s vs 4.66–6.09 s。

构建过程中发现并已写入代码注释的两个 SQLite 行为：嵌套在上层 upsert 中的触发器里用 `OR IGNORE`/`OR REPLACE` 会报 UNIQUE 而不是消解冲突（改用显式 `NOT EXISTS` 守卫）；因此触发器定义必须 `DROP` 后重建，只加 `IF NOT EXISTS` 会让升级后的库永久保留旧定义。

### 2. 删除失效检索投影

先做**已验证备份**（`backups/vector_lake_1789575103.db.bak`，3 622 MB，`integrity_check ok`，并从备份里回读行数），再删除 `operational_memory_search_docs/_fts/_short_fts/_pending/_state/_revision` 与 `search_projection_state_v8`，以及仍在维护它们的 10 个 `trg_operational_memory_search_*` 触发器。表 81→66、触发器 72→62，释放 49 547 页（193 MB）回 freelist；`integrity_check` 与 `foreign_key_check` 均干净；`operational_memory`(142 117)、`claims`(120 339)、`timeline_events`(9 286) 均未变。

### 3. `page_index_projection`：index.json 投影为节点表 + 邻接表

读路径原本每个进程解析整份 18 MB 文件，而且**每次查询**都重建 30 140 条边的邻接字典。`tracemalloc` 实测：解析后常驻 **42.2 MiB**、峰值 **137.4 MiB**；投影邻接仅常驻 **9.7 MiB**。现在节点按 key 取（每次查询几十行），7 178 个节点的字典不再整体存在于内存；`assemble_context` 的 50 行摘要是 50 行 SQL。

`index.json` 仍是主权制品：`page_index_state(index_mtime, index_size)` 用一次 `stat` 就发现带外改写，投影自行修复。`page_index_edges` 带显式 `sequence` 列，因为两步 personalized PageRank 对顺序敏感；节点字典、边顺序与最终邻接均已与改动前的内存内构建在线上语料逐项核对一致（7 178 节点、30 140 边）。

### 验证

- `pytest tests -q` → **487 passed**（476 原有 + 11 新增 `tests/test_memory_gram_index.py` / `tests/test_page_index_projection.py`）。
- `ruff check vector_lake` 错误总数与改动前一致（65 条均为既有 E402/E701）。
- 线上不变量：memory 投影 142117/142117、drift 0/0；timeline parity 9286/9286；`integrity_check` ok。
- 新增运维面：`python cli.py gram-index [--apply|--compact]`、MCP `memory_gram_index_status` / `rebuild_memory_gram_index` / `compact_memory_gram_index`。

### 未完成

- **另一套失效机制仍在，超出本次授权范围**：20 张表上的 60 个 `trg_*_generation_v3_*` 触发器与 `projection_runtime_v9` 在本仓库**既无创建方也无读取方**（已 grep 验证），`runtime_generations` 同样无人引用；它们仍在每次写入时维护一个无人消费的 generation 注册表。删除属破坏性操作，需单独决策。
- 数据库 3 887 MB，其中 **352 MB 在 freelist**（重建暂存表留下的空间）。`VACUUM` 可回收，但需要数分钟排他锁，留给运维决定。

# Vector Lake 11.19.0

## 读路径性能：search / query / timeline 在 8 488 页语料上从 14.3 s 降到 1.9 s

完整测量与证据见 `PERF_2026-09-16.md`。全部改动都是实测定位到的瓶颈，不是预防性重构。

实测语料：8 488 个 wiki 页、142 117 条 `operational_memory`、120 339 条 claim、9 286 条 timeline 事件、`index.json` 18.0 MB、`vector_lake.db` 3.8 GB。

| 路径 | 改前 (p50) | 改后 (p50) | 倍数 |
|---|---|---|---|
| `query` → `assemble_context` | 14.25 s | **1.91 s** | 7.5× |
| `build_memory_packet` | 10.50 s | **1.67 s** | 6.3× |
| `search_operational_memory` | 5.00 s | **0.72 s** | 7.0× |
| `search`（page 模式） | 2.53 s | **0.22 s** | 11.5× |
| `search`（memory 模式） | 5.72 s | **0.77 s** | 7.4× |
| `timeline` | 0.75 s | **0.05 s** | 15.4× |

### 1. `operational_memory_index` 检索投影（主要收益）

`search_operational_memory` 原本是**全表 Python 扫描**：`SELECT *` 后对 142 117 行逐行 `json.loads`（cProfile：加载 4.5 s、评分 0.9 s），而 `build_memory_packet` 为了同时取“活跃视图”和“含历史视图”**把它跑了两遍**。

修复分两步：

- `search_memory_packet_views` 用一次加载喂两个视图；`assemble_context` 与 `_search_scored_pages` 共用 `_load_index_cached`，不再为 50 行摘要二次解析 18 MB 的 `index.json`；
- 新增 `operational_memory_index` 投影表（`db_store`），由 `AFTER INSERT/UPDATE/DELETE` 触发器**在数据库层**维护，因此任何写入方（包括 `governance_store` 之外的代码）都无法绕过。检索改为 SQL 侧精确评分（`_memory_relevance` 的逐字重述：相关性为 0 的行才被丢弃，与 Python 评分器一致），SQL 取窗口后**仍用原 Python 评分器重排**，并按 `source_rowid` 复原 `SELECT *` 的自然序作为末位 tie-break。

`VECTOR_LAKE_MEMORY_SEARCH=legacy` 可强制回到旧全表扫描；它既是差分测试的 oracle，也是运维逃生口。`tests/test_operational_memory_index.py` 对 10 组查询/过滤组合断言两条路径**逐条 id 完全一致**。

自愈：`PRAGMA data_version` + `Connection.total_changes` 门控决定是否运行行数对账（未变动时约 20 µs，而非两次 `COUNT(*)` 扫描），投影被删或落后会在下一次查询前修好。

### 2. Timeline：修掉一个长期报错，并把校验成本降到常数级

- **回归修复**：canonical 回退路径引用 `claims.entity_id`，而该列**不存在** → 任何带 `entity_name`/`sentiment`/`action` 的 timeline 查询都返回 `Error executing timeline query: no such column: entity_id`。改为对 `data_json.subject_entity_ids` 过滤，并补齐实体标题映射。
- **实测发现投影已漂移**（`missing=9 284 / extra=9 284`），意味着索引路径长期不可达、每次都走坏掉的回退。已执行 `rebuild_timeline_events_from_claims`，parity 归零。
- `timeline_projection_parity` 每次查询都要全扫 claim 并逐条 SHA-256（0.79 s / 0.80 s）。新增 `idx_claims_claim_type` 表达式索引，并按“库路径 + claim 指纹 + 投影 id 极值”做记忆化；任何写入方（含其他进程）都会改变指纹。
- 投影的 `event_date` 此前对 6 573 / 9 286 条事件取了**入库时间**：事件日期其实写在 claim 文本的 `[YYYY-MM-DD]` 前缀里。`claim_event_date` 现在按 anchor → 文本前缀 → `updated_at` 取日期，`ORDER BY event_date DESC` 才有意义。
- 投影漂移时输出 `[DEGRADED]` 前缀说明来源，降级结果不再被误认为权威结果。

### 3. 查询向量：客户端复用 + 有界缓存

`embed_texts` 每次请求都 `genai.Client(...)`（本机实测 1.7–6.9 s），占了查询向量 2.4 s 延迟中的约 1.7 s。改为进程内复用一个客户端，缓存以**工厂函数身份**为键，因此 monkeypatch 重绑 `_create_client` 依然生效。查询向量另加 256 项 LRU（按 float32 存，约 3 MB 上限），只缓存成功结果。

### 4. 验证

- `pytest tests -q` → **450 passed**（429 原有 + 21 新增）。
- `ruff check` 在改动文件上与改动前错误数一致（4 条全部为既有 E402/E701）。
- 差分等价：索引后端与 legacy 全表扫描在 `top_k`、`include_history`、`memory_types`、空查询等组合下返回**完全相同**的 id 序列。
- 复现脚本与逐项证据：`benchmarks/bench_hot_paths.py`、`PERF_2026-09-16.md`。

### 5. 未完成 / 遗留

- **长 CJK 查询仍非秒级**：`_query_terms` 会展开全部单字与相邻 bigram，30 字查询产生约 59 个词项、约 2.0–2.5 s 的 `instr()` 工作（旧路径 6.97 s，改善 2.8×）。进一步提速需要 n-gram 倒排索引或 FTS5/trigram 投影，两者都会改变检索语义，因此保留为待决策项而非静默混用。
- **失效投影仍在被维护**：`operational_memory_search_docs`（128 615 行）/ `..._fts` / `..._short_fts` / `..._pending`（积压 13 788 行）/ `..._state` / `search_projection_state_v8` 在本仓库中**既无创建方也无读取方**（已 grep 验证），消费端随本地树成为主线而丢失；但它们**不是惰性数据**：线上库仍装着 `trg_operational_memory_search_insert/update/delete` 与 `..._revision` 触发器，因此每次 `operational_memory` 写入仍会往 `..._pending` 入队、并为一个无人读取的投影递增 revision。实测占用：FTS 数据块 ≈162 MB（`..._fts_data` 97.8 MB + `..._short_fts_data` 64.3 MB）加影子表行。删除表与触发器属破坏性操作，需明确授权与已验证备份后另行执行。
- **写路径成本**：新触发器使 `operational_memory` 每行写入增加约 **118 µs**（20 000 行插入：无该触发器 0.24 s，有 2.61 s）。增量入库只重写变更 claim 的 memory 及其直接冲突对端（每页数十行），折合约 10–15 ms/页；可见代价是全量 `rebuild_operational_memory` 在 142 k 行语料上慢约 17 s。若日后成为瓶颈，可改用 pending 队列入队式触发器把成本移出写路径。
- `index.json`（18 MB）仍整体解析；10 万页规模需要改为投影邻接表。

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
