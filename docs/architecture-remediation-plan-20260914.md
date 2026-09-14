# VECTOR_LAKE 架构整改方案与执行计划

- **状态**: 已批准执行（2026-09-14）。P0 已落地；P1 起为待执行
- **决策记录**:
  - `2026-09-14` 作者确认 **P0 硬前置已解决**：既有 WIP 提交为 `61830d1`（迁移基线因此干净）。
  - `2026-09-14` 作者**接受 P2 的跨库原子性代价**：规范写入与读模型发布不再共享同一 SQLite 事务，转为显式发布协议。P2 因此解除阻塞，但仍按"最小表先验证"顺序执行。
  - `2026-09-14` **更正审计自身的一处误报**：原判 "`schema.md` `Schema V8.0` 与 `_SCHEMA_VERSION = 9` 漂移" **不成立**。二者是独立版本轴：`user_version` 是 SQLite 存储 schema，`Canonical governance schema 8.0` 是 `schema.md` 文档自身的版本（代码中不存在对应常量）。README 该表在两个轴上各自自洽。P0.1 范围据此收窄（见下）。
  - `2026-09-14` 作者**确认停机窗口**。实际执行时发现**该窗口对 P1 的完整性目标不再必要**（见 P1 机制变更），因此**未开启窗口、未触碰活库**。保留的、真正需要窗口的只有两项可选动作：`entities.ttl`/`decay_weight` 的 `DROP COLUMN`（破坏性 DDL），以及打开 `foreign_keys`（需先测代码路径）。
  - `2026-09-14` 停机前盘点实测（已修正先前误记）：`.meta` 实际为 **3.81 GiB / 76 文件**——先前 `du -sh .meta` 报 7.5 GB **不是** git-bash 重复计数（那是错误解释，已撤回），而是一次真实的瞬时占用：`.meta/backups/` 下 **2,782 个备份文件（3.9 GB）在 18:57 前后被按龄保留策略删除**，删除过程中的 18:56 读数 = 3.6 GB 库 + 3.9 GB 备份 = 7.5 GB，删除完成后的 19:00 读数 = 3.9 GB（仅库）。证据：`backups/` mtime `18:57` 且现为空；`storage_growth.json` 的 18:04 样本仍记 `backup_bytes 3,899,415,957 / 2,782 files`；`tool_backup_retention._apply_retention_plan` 具备 unlink 目录能力。
  - `2026-09-14` **备份状态需在开窗前人工确认（风险）**：`doctor_vector_lake(mode='quick')` 的**生效策略**为 `maintenance_backup_mode: "skip"`（非 `runtime_profiles.json` 声明的 `full`），且两个 backup root 均为 `0 文件 / 0 字节`——**当前系统没有任何备份**。缓解事实：`backup_capacity.maintenance_backup_mode()` 的文档字面语义说明 `skip` "never reaches the operations that read the backup back as a verified input: those call `tool_projection.require_maintenance_backup` and behave identically under either mode"，即**迁移自身的 pre-DDL 备份不受 `skip` 影响**。容量侧不阻塞：quota 12 GiB / `enforce`，磁盘空闲 3.05 TiB，要求保留 ≥ 409 GB，均满足。
- **依据**: 2026-09-14 只读架构审计（未落盘为文档；本文件所有数字均为该次审计中在 `~/MEMORY/wiki/.meta/vector_lake.db` 与源码 AST 上的实测值，末尾附复现命令）
- **基线**: 源码 `f08421b` + 工作树 35 个未提交改动
- **版本目标**: 11.20.0 → 11.21.0

---

## 1. 总原则

1. **停机只付一次。** 规范库的结构性修改全部并入**一个** v10 迁移窗口，不拆成多次 stop-the-world。
2. **读模型靠重建，不靠迁移。** 派生表全部可从规范数据重建（`projection_rebuild_index` / `embedding_backfill` / `operational_memory_search_index` / `canonical_backfill` 已存在），因此不需要停机窗口，用"重建 → 切换读路径 → 删旧表"完成。
3. **先断环，再拆模块。** 在 37 模块强连通分量被打破之前拆分 god module，拆出的模块会立刻重新成环。
4. **每批 ≤ 10 个文件。** 符合 SOUL.md 突变断路器；超出则拆批。
5. **宵禁 22:30–06:00 不执行**变更类动作（只读探针不受限）。
6. **不重写。** 本计划不改任何业务语义，只改边界、完整性归属和权威归属。

---

## 2. 硬前置条件（P0 之前必须完成）

**工作树必须干净。** 当前 35 个未提交改动（最新 mtime `18:10:19`，涉及 `watchdog_app.py` / `runtime_health.py` / `governance_store.py` / `mutation_coordinator.py` / `db_store.py`）。这些文件正是 P1/P2 的 write-set。

若带着脏工作树执行 v10 迁移：迁移 receipt 与 recovery bundle 会绑定到不确定的源码状态，回滚时无法复现。**这是不可绕过的前置条件**，不由本计划处理，需作者决定提交、另开分支或 stash。

**✅ 已解决（2026-09-14）**：WIP 提交为 `61830d1`「Bound Wiki reconciliation; land the off-by-default host relay consumer」。提交前全量验证：ruff `E4,E7,E9,F` clean、`compileall` clean、`pytest` **2968 passed / 2 skipped / 320 subtests passed**（11m37s）。提交时从两个新增测试文件移除了未使用的 `import json` / `import pytest`（否则 CI ruff 门失败）；除该 2 行外未改写作者任何行为。

---

## 3. 阶段划分

### P0 — 止血（无迁移窗口，4 批）

| # | 动作 | 文件 | 验收 oracle |
|---|---|---|---|
| P0.1 | **契约文档漂移修复**（范围已更正）：`README.md:47` 的 `INGEST_CONTRACT_VERSION = 5` → `6`（源码 `tool_ingest.py:2649`）。同一常量在 README 正文还有 5 处陈旧引用（`:180` `:181` `:202` `:208` `:395` `:755` 的 "ingest v5"），一并改为 v6；其中 `:208` 关于\"较早契约版本活动任务在领取前受控重建\"的表述按代码事实重写（领取过滤器要求 `ingest_contract_version` 等于当前版本，见 `db_store.py:11056`）。**不含** `schema.md` 版本号——那是独立版本轴，非漂移 | 2 | `pytest tests/test_release_metadata.py` |
| P0.2 | **把契约号纳入自动化门**：`tests/test_release_metadata.py` 新增 3 个测试——README Runtime Contract 表的契约号必须等于源码常量（ingest / `user_version` / projection / EvidencePacket）、公开表面计数必须等于活体定义（70/9/21 MCP、43 CLI、19 skills）、治理 schema 与 `user_version` 必须是可区分的两个轴且两份文档互洽。已做**反证验证**：注入漂移后测试确实失败 | 1 | 反证通过（见 §6） |
| P0.3 | **图边对账（实测缺陷）**：`claim_graph_edges=10,293` vs `page_graph_edges=10,400`，only_page=133 / only_claim=26，其中 130 行**双端都存活**（非孤儿，不能盲删）。先用 `tool_legacy_graph_audit` 跑出权威判据，再生成幂等对账脚本走既有治理修复入口 | ≤3 | 两表集合差归零；`tool_legacy_graph_audit` 的 `current_relation_graph_dual_write_divergence` blocker 消失 |
| P0.4 | **写闸门降级**：`runtime_health.py:2225-2240` 的 `write_projection_drift` 目前进 `issues` → `ok = not issues` → `enforce_runtime_write_health` 对**全体写入者**抛错。改为：按页隔离 + 触发 outbox 修复；只有规范数据损坏（`sqlite` integrity、generation 失配）才保留全局 fail-closed | ≤3 | 新增测试：注入单页漂移后，**该页**被隔离且**其他页**写入成功 |

**P0.3 的判据补充（实测，供决策）**：`only_page` 关系分布 `validates 78 / related_to 15 / instantiated-by 10 / evolved-from 8 / depends-on 6 / part-of 5`；样例 `Vendor_全国卫生标准技术委员会 → Institution_全国卫生标准技术委员会 (evolved-from)` 形态指向"改名/改类型后旧边残留"，但 130 行双端存活意味着**也存在合法新增未落另一表的方向**。因此判据不能是"删多的"，必须由 `governance_store` 的 change-set 作为权威回放。

---

### P1 — 规范表不变量（机制已修订：**无需停机**）

> **⚠ 机制变更（2026-09-14，基于实测）**：下文原定的"5 张表 12 步重建 + NOT NULL/CHECK"**已被更便宜的等价方案取代**。保留原文以便审计差异。

**修订后的结论**：完整性目标（"DB 自身拒绝非法状态"）可以用 **`CREATE TRIGGER IF NOT EXISTS`** 达成，**无需表重建、无需数据拷贝、无需停机窗口**。

实测依据：

| 事实 | 证据 |
|---|---|
| 触发器能拒绝 NULL（INSERT 与 UPDATE 均生效） | 在临时库上实测：非法插入/更新均 `IntegrityError: claims_not_null_violation` |
| 幂等可重放，适合放在 bootstrap 路径 | `CREATE TRIGGER IF NOT EXISTS` 重放无副作用，与 `db_store.py:8097` 等 `CREATE TABLE IF NOT EXISTS` 同一模式 |
| **仓库已在用这个模式**维护最强的不变量 | `trg_canonical_identities_owner_conflict`、`trg_canonical_identities_append_only_update/delete`（`db_store.py:374-393`）、`trg_change_set_terminal_v6_immutable`（`:543`）、`trg_operational_memory_search_*`（`:7659-7696`） |
| 新增触发器不会被 schema 契约拒绝 | 唯一的 glob 断言是 `name GLOB 'trg_*_generation_v*_*'`（`db_store.py:879-880`），只需用不撞该命名空间的触发器名 |
| SQLite 3.50.4 / Python 3.13.12 | `DROP COLUMN` 支持（≥3.35）；**无 `ADD CONSTRAINT` 语法**⇒ NOT NULL 确实需重建，但触发器绕开了该限制 |

**已知爆炸半径**：`tests/test_db_transactions.py` 约 8 处回滚夹具只写 `entities(entity_id, data_json)`，会因不变量而失败。经核对它们是**测试夹具而非生产路径镜像**（生产走 `governance_store.upsert_entity`，显式列出全部列，故活库实测 0 NULL）。属于有界、正当的测试更新。

---

## 以下为原文（已被上文取代，保留用于审计对比）

### P1（原计划）— 一次性 schema v10 迁移窗口（完整性与权威归属）

**目标断言**：规范库自身能拒绝非法状态；同一个事实在 `entities` 行内只有一个权威。

**实测支撑（这是本阶段低风险的原因）**：

```
claims(106,724) / entities(7,905) / evidence(139,194) / sources(4,071) / operational_memory(128,502)
   当前列上 NULL 行数 = 0                     → NOT NULL 迁移零违规
entities.type     非空 7,905/7,905 (100.0%)    → 保留为权威
entities.status   非空 7,905/7,905 (100.0%)    → 保留为权威
entities.ttl      非空     5/7,905 (  0.1%)    → 死列
entities.decay_weight 非空 5/7,905 (  0.1%)    → 死列
读路径实际取值: indexer.py:1605 / governance_store.py:1350 均读 data_json
```

**变更内容**：

1. 五张规范表加 `NOT NULL` + 值域 `CHECK`（`claims` 106,724 / `evidence` 139,194 / `sources` 4,071 / `entities` / `operational_memory`）。
   - SQLite 不支持 `ALTER TABLE` 给已存在列加约束，必须走 12 步表重建 → 只能作为一次 schema 版本完成，这正是并入单一窗口的原因。
2. **删除 `entities.ttl` 与 `entities.decay_weight`**。二者 99.9% 为 NULL 且无生产读路径，是唯一被实测到的"同事实双权威"（`col≠json` 5,726 行中 5,725 行是列侧 NULL）。保留它们等于保留一个必然再次漂移的面。
3. `entities.type` / `status` 明确**以列为权威**，写入路径从 `data_json` 剥离这两个键（当前 0 不一致，剥离是纯收敛）。
4. `PRAGMA foreign_keys` 由 0 改为 1，为已声明 `REFERENCES` 的 5 张表（`change_set_lifecycle_v6` / `change_set_payload_refs` / `ingest_outbox_links` / `embedding_jobs` / `projection_outbox`）补 `ON DELETE` 语义。
5. 迁移实现点（照既有模式登记，不自创机制）：
   - `db_store.py:775` `_SCHEMA_VERSION` 9 → 10
   - `db_store.py:776` `_SCHEMA_MIGRATIONS` 增加 v10 条目
   - `db_store.py:105` `_SCHEMA_MIGRATION_SUPPORTED_SOURCE_VERSIONS` 增加 10
   - `db_store.py:3926` `_schema_migration_steps(10)` 增加步骤
   - `_SCHEMA_ROLLBACK_MIGRATION_BINDING_KEYS` 登记 v9→v10 回滚绑定

**执行规程（复用既有，不新增）**：

```bash
# 1. 停止 MCP / watchdog / 其他 SQLite 写入者
# 2. preview（只读）
python cli.py schema-migrate --action preview
# 3. 若报告 database_has_uncheckpointed_wal，以同一 fingerprint checkpoint 后重新 preview
# 4. apply（持有 schema maintenance lock 并重新核验 fingerprint）
python cli.py schema-migrate --action apply --fingerprint <preview 返回>
# 5. 迁移后必须重建投影（迁移不会自动做）
python cli.py projection-rebuild-index
```

**验收证据**：
- `PRAGMA user_version = 10`；`PRAGMA foreign_keys` 生效；`PRAGMA quick_check = ok`
- 行数对账：五张表迁移前后逐表行数一致；`entities` 列集合 = 预期集合
- 故意插入 NULL / 非法 `status` **必须被 DB 拒绝**（新增测试，这是本阶段的真实 oracle）
- `pytest -q` 全绿；`benchmarks/corpus_scale_benchmark.py --nodes 10000 --fail-on-slo` 通过
- `schema-rollback` 演练：v9→v10 receipt 可回滚并重新通过关键验收

**回滚点**：迁移前 `quick_check` 验证的 SQLite 备份 + 迁移 receipt 绑定的 recovery bundle（既有机制）。

**风险**：表重建期间的写入者停机；`entities` 死列删除需确认无隐藏读点（已 grep，无）。

---

### P2 — 读模型迁出规范库（无停机，按表增量）

**目标断言**：规范库 schema 版本不再被读侧需求驱动。

**待迁出表（实测行数）**：

| 表 | 行数 | 可重建入口 |
|---|---|---|
| `operational_memory` | 128,502 | `canonical_backfill` / `operational_memory_search_index` |
| `operational_memory_search_docs` | 128,502 | `operational_memory_search_index` |
| `operational_memory_search_{pending,revision,state}` | 0 / 1 / 1 | 同上 |
| `claim_graph_edges` | 10,293 | `projection_rebuild_index` |
| `page_graph_edges` | 10,400 | `projection_rebuild_index` |
| `claim_graph_nodes` | 0 | 同上 |
| `search_projection_state_v8` | 1 | 同上 |
| `projection_runtime_v9` | 1 | 同上 |
| `embedding_metadata_v8` / `embedding_runs` / `embedding_rate_reservations` | 0 / 29 / 1 | `embedding_backfill` |
| `runtime_generations` | 15 | 从规范 generation 推导 |
| `schema_registry` | 0 | 迁移期重建 |

**为什么无停机**：这些表全部是派生物，**重建比迁移便宜**。流程：新建 `vector_lake.read.sqlite` → 从规范数据重建 → 读路径双写期（读新、比对旧）→ 切换 → 删旧表。**不进入 schema-migrate 停机窗口。**

**必须诚实说明的代价**：今日"规范写入 + 读模型发布"在**同一个 SQLite 事务**内获得隐式原子性（`change_set` + `outbox` + 读模型同步提交）。拆库后该保证消失，必须转为**显式发布协议**。可行性依据：`projection_runtime_v9` 的 `rebuild_required / publish_pending / ready` 状态机已经是这个协议，本阶段是把它从"同库内的隐式保证"变成"跨库的显式契约"。**这是本计划唯一引入新风险的地方**，因此：

- 先迁 1 张低风险表（`embedding_runs`，29 行）验证协议，再迁 `operational_memory`（最大），最后迁 `projection_*`（最敏感）。
- 每张表独立批次，独立回滚。

**验收证据**：
- 每张表：双写期比对零差异，且差异注入能被检测
- `search_vector_lake` / `recall` / `trace_vector_lake` / `projection_report` 在切换前后返回一致结果（同一查询集逐条比对）
- 跨库发布协议在崩溃注入下保持 old-or-new 完整代（复用既有故障注入测试）
- 规范库中该表已删除；规范库 schema 只保留规范关注点

---

### P3 — 打破 37 模块强连通分量（38 条反向边 / 64 个符号）

**这是本计划工程量最集中的阶段，也是最可量化的。**

我按目标分层对 310 条层内依赖边做了全量比对：**38 条违反层序（12.3%）**。打破循环 = 处理这 38 条边，涉及 **64 个符号**。不是重写。

目标层次（自下而上）与违规分布：

```
L0_base          runtime_paths wiki_utils yaml_utils durability cancellation
                 memory_protocol tokenizer_runtime
L1_storage       db_store governance_store projection_store_v2 projection_format_v2
                 raw_revision storage_growth
L2_domain        schema_validator purpose_contract claim_extractor ... defense_hook
L3_derived       indexer runtime_health governance_metrics backup_capacity ...
L4_orchestration mutation_coordinator watchdog_app auto_ingest_worker ingest_worker ...
L5_handler       tool_* governance_service auto_ingest_runners.*
L6_surface       mcp_server cli_app
```

| 违规方向 | 边数 | 处理类别 |
|---|---|---|
| L1_storage → L2_domain | 10 | (a) 纯函数下移 |
| L4_orchestration → L5_handler | 6 | (b) 接口反转 |
| L1_storage → L5_handler | 3 | (b)(c) |
| L2_domain → L3_derived | 3 | (b)(a) |
| L0_base → L5_handler | 3 | (b) |
| L3_derived → L5_handler | 3 | (b) |
| L1_storage → L3_derived | 2 | (a) |
| L0_base → L3_derived | 2 | (b) |
| L0_base → L2_domain | 2 | (a) |
| 其余单条 (L2→L5, L3→L4, L5→L6, L0→L4) | 4 | (b)(c) |

**处理类别（决定难度）**：

**(a) 纯函数下移 —— 最低风险，占比最大。** 实测 `L1_storage → L2_domain` 的 10 条边大多是**工具函数被放错了层**，不是真正的层间契约：

```
governance_store -> memory_search_normalization : casefold_text
governance_store -> evidence_foundation         : version_family_id
governance_store -> source_references           : normalize_explicit_source_page_ref
db_store         -> search_projection_contract  : encode_fts_corpus_row
governance_store -> operational_memory_contract : effective_routing_type
governance_store -> claim_extractor             : classify_non_claim_text, extract_page_objects
```

这些是纯函数，应下移到 L0。**移动 + 改 import 即可，无控制流反转。**

**(b) 接口反转 —— 中等风险。** 真正的结构性依赖，需要依赖注入或回调注册：

```
schema_validator -> indexer                      # 校验器依赖索引器
wiki_utils -> schema_validator / defense_hook / mutation_coordinator
tool_doctor -> mcp_server                        # 唯一 L5→L6
runtime_health -> watchdog_status
db_store / governance_store -> tool_timeline, tool_ingest   # 存储层依赖工具层
memory_protocol -> tool_memory / tool_query / tool_search
runtime_health -> tool_auto_ingest / tool_gc / tool_timeline
auto_ingest_worker / ingest_worker / watchdog_app -> tool_ingest
```

其中 `db_store → tool_ingest`、`governance_store → tool_timeline`、`tool_doctor → mcp_server` 是明确的错误方向，必须反转。`L4_orchestration → L5_handler`（6 条）建议做法：把 `tool_ingest` 等模块中被 worker 复用的实现下沉到 L4，`tool_*` 只保留参数校验与入口适配。

**(c) 常量/枚举提取。** 反向边若只导入常量或异常类，提取到独立小模块。

**分批（每批 ≤10 文件，独立 commit + 独立验收）**：

| 批 | 范围 | 边数 | 验收 |
|---|---|---|---|
| P3.1 | 引入 `importlinter` 契约（目标层次声明）+ CI 接入，允许现有 38 条为 `ignore_imports` 白名单 | 0 | CI 报告 38 条，白名单之外归零 |
| P3.2 | (a) 类纯函数下移：L1→L2 十条 + L1→L3 两条 | 12 | 该批白名单条目删除；全量测试绿 |
| P3.3 | (a) 类：L0→L2、L0→L3 | 4 | 同上 |
| P3.4 | (b) 类：存储层→工具层（`db_store`/`governance_store` → `tool_ingest`/`tool_timeline`） | 4 | 同上 |
| P3.5 | (b) 类：L4→L5 六条 + `tool_doctor → mcp_server` | 7 | 同上 |
| P3.6 | (b) 类：`memory_protocol` 三条 + `runtime_health` 四条 | 7 | 同上 |
| P3.7 | 剩余单条（`schema_validator→indexer`、`wiki_utils→*`、`claim_assessment→governance_metrics` 等） | 4 | 同上 |

**完成判据（不是 0 违规，而是）**：
- `importlinter` 层次契约在 CI 强制，白名单为空
- AST 全量分析：**不存在超过 8 个模块的强连通分量**
- 函数体内延迟 import 计数由 **223** 显著下降（目标 ≤80）；剩余每一处有书面原因

**为什么必须先于 P4**：现在拆分 `db_store`（13,059 行 / 271 顶层函数）会立刻产生新的环。断环是拆分的先决条件。

---

### P4 — 拆分 god module 与去浏览器化投影契约

**P4.1 拆分 `db_store.py`（13,059 行 → ≤6 个模块，按职责非按行数）**

候选切分（沿既有函数簇）：schema/迁移与校验、连接与事务、规范 CRUD（entities/claims/evidence/sources）、outbox、投影发布状态、FTS/搜索运行时。

每刀独立批次，≤10 文件，验收 = 模块对外接口不变 + 全量测试绿 + 无新增层序违规。

**P4.2 投影截断显式化**（`projection_format_v2.py:49` `MAX_CLAIM_GRAPH_NODES = 2_500`，成因注释 `governance_store.py:5058` "Hard cap to prevent 3D-force-graph from freezing the browser"）

实测：manifest 报 `claim_nodes 2500`，规范侧 `claims 321,520`、`entities 14,555`——**图投影只承载 0.78% 且无截断标记**。

动作（**加字段，非破坏格式**）：
- manifest 增加 `truncated: true` + `total_nodes` + `selection_policy`（当前为度限制选择）
- `indexer.py:1941` / `tool_gc.py:188` / `tool_legacy_graph_audit.py:721` 等消费者在结果中透出截断状态
- **不建议**直接把上限抬高——那是投影 v2 格式契约变更，需要迁移；先把"样本被当成全图"这个误用面消掉

**P4.3 Runtime Contract 表由常量生成**（P0.2 是手写断言的权宜；本步改为从 `_SCHEMA_VERSION` / `INGEST_CONTRACT_VERSION` / manifest 生成 README 该表）

---

### P5 — 规范与指令分离

**问题（已验证）**：`tool_ingest.py:4280-4296` 把 `schema.md` + `SCHEMA_CATEGORIES.md` 读入，经 `tool_ingest.py:4320` `{{schema_content}}` 注入生成器提示；而 `schema.md:4` 的字面内容是 `[CRITICAL SYSTEM OVERRIDE]`。同一拼装体还包含 `{{index_summary}}`（模型自己写过的页面标题/摘要）与不可信 raw 文本，接收方持有 `finalize_ingest` 写权限。

**动作**：
1. `schema.md` 拆为 `contracts/schema-contract.md`（纯数据规范，无第二人称指令）与 `templates/ingest-generator-instructions.md`（生成器指令）
2. 注入路径只取规范文件；生成器指令作为模板的一部分，不与规范拼接
3. 新增测试：断言注入文本中不含 `[CRITICAL`、`SYSTEM OVERRIDE`、`You are` 等指令形态串（防回归）

**收益**：规范变更与生成器行为变更重新可分离审计。

---

## 4. 排序理由（依赖图）

```
[硬前置: 工作树干净] ✅ 61830d1
        │
      P0 止血 ✅ P0.1/P0.2 (fb47fd3)；P0.3/P0.4 未开始
        │
      P1 规范表不变量 —— 机制修订为触发器，无停机
        │                 （仅 DROP COLUMN / foreign_keys 仍需窗口，可选）
        │
      P2 读模型迁出 (无停机，逐表) —— 决策已接受
        │
      P3 断环 ← 必须先于 P4.1
        │
      P4.1 拆 db_store ── P4.2/P4.3 独立可并行
        │
      P5 规范/指令分离 (独立，可随时插入)
```

- **P0.4 与 P1 的相对顺序**：P0.4 降低写闸门强度，P1 是数据迁移。先做 P0.4 可以让 P1 迁移过程中的临时不一致不至于全局阻塞写入。建议 P0.4 先于 P1。
- **P1 与 P2 必须串行**：都改规范库 schema，同一时刻只有一个写者（C-CONC-01）。
- **P5 可完全独立并行**：仅涉及 `schema.md` 与 `tool_ingest.py` 注入路径。

---

## 5. 批次与规模

| 阶段 | 批次数 | 文件改动上限/批 | 是否需停机 | 风险 |
|---|---|---|---|---|
| P0 | 4 | ≤3 | 否 | 低（P0.4 为行为变更，中） |
| P1 | **0（原为 1 窗口）** | 8 | **否（机制修订后）** | 低（触发器）／中高（若坚持表重建） |
| P2 | 4（每张表一批） | ≤10 | 否 | 高（跨库原子性），故最小表先验证 |
| P3 | 7 | ≤10 | 否 | 中（P3.2/P3.3 低） |
| P4 | 6–8 | ≤10 | 否 | 中 |
| P5 | 2 | 4 | 否 | 低 |

总计约 **26–30 个独立批次**。每批次独立 commit、独立验收、独立回滚。

---

## 6. 每阶段通用验收门

**确定性 oracle（存在即必须执行，C-VALID-01）**：

```bash
python -m ruff check --no-cache --isolated --select E4,E7,E9,F vector_lake tests
python -m compileall -q vector_lake tests
python -m pytest -p no:cacheprovider -q
python benchmarks/corpus_scale_benchmark.py --workspace . --nodes 10000 \
    --serial-queries 20 --concurrent-queries 40 --workers 4 --fail-on-slo
```

**类型与层次门（P3.1 后新增）**：目录尚未引入类型检查，虽然代码已大量使用 `str | os.PathLike[str]` 注解但从未被验证。P3.1 至少补 `importlinter`；是否引入 mypy 建议单独决策（不在本计划范围，因为仅注解风格统一就可能产生数百条告警）。

**反证验证（P0.2 已执行）**：新增的门必须真能失败，否则它不是 oracle。二者均已实测：

| 注入 | 结果 |
|---|---|
| README `INGEST_CONTRACT_VERSION = 6` 改回 `5` | `test_readme_runtime_contract_numbers_match_their_source_constants` **FAILED** |
| README `70 MCP tools` 改为 `69 MCP tools` | `test_readme_public_surface_counts_match_the_live_surfaces` **FAILED** |
| 恢复原值 | 7 passed |

**独立审查门（SOUL.md / coding.md §4）**：P1（有状态数据迁移）、P2（跨库原子性）、P3.4–P3.7（结构反转）属 L3/Refactor，须由**未参与生成**的 reviewer 复核；能力不可用时进入 BLOCKED，自审不构成独立批准。

**用户改动保护**：每批 Mutate 前后核对工作树，确认未覆盖用户改动（当前有 35 个待处理）。

**回滚**：P1 用既有 backup + receipt；P2 每表独立切换点，保留双读期；P3/P4 每批为独立 commit。

---

## 7. 预期效果（可度量）

| 指标 | 当前实测 | 目标 |
|---|---|---|
| 最大强连通分量 | **37 模块** | ≤ 8 |
| 违反目标层序的依赖边 | **38 / 310 (12.3%)** | 0（CI 强制） |
| 函数体内延迟 import | **223** | ≤ 80，其余有书面原因 |
| 规范表上的 NOT NULL/CHECK | **0** | 全覆盖，非法状态由 DB 拒绝 |
| `PRAGMA foreign_keys` | **0** | 1 |
| 同一行双权威字段 | `entities.ttl`/`decay_weight`（99.9% 空） | 0 |
| 超过 5,000 行的模块 | `db_store` 13,059 / `governance_store` 9,700 / `watchdog_app` 4,343 | 全部 ≤ 5,000 |
| 规范库 schema 被读侧驱动的版本数 | v6,v7,v8,v9（**4 个**） | 0 |
| 图投影截断可见性 | 2,500/321,520 无标记 | manifest 显式声明 |
| 派生表驻留规范库 | 12 张 | 0 |
| **投影对象库垃圾占比** | **已实测（修正原先的 UNVERIFIED）：扫描 6.28 GB 中可达仅 99.6 MB，孤儿 6.18 GB → 至少 98.4% 是垃圾**；且扫描本身在 200,000 文件处截断（`projection_object_file_limit_exceeded`，`scan_complete: false`），真实孤儿量更大 | 由 GC 回收（当前 `dry_run=True` 为默认） |
| 契约文档与源码一致 | `INGEST_CONTRACT_VERSION` 表内 1 处 + 正文 5 处陈旧（`schema.md` 版本号经复核**不是**漂移） | 由测试强制一致 |

**注意**：本计划**不改变** `mutation_outbox` 保留 27,643 条 completed（9 周）导致的库体积增长，也不改测试/源码 LOC 比（84,711 : 93,463）。两者都是独立问题，建议单独决策，不混入本计划以免扩大爆炸半径。

---

## 8. 明确不做

| 项 | 理由 |
|---|---|
| 重写任一子系统 | 现有设计（outbox + 哈希 CAS + generation CAS + receipt）本身成熟；缺的是边界不是机制 |
| 一次性大重构 | 违反 ≤10 文件断路器；且 P2/P3 的相对顺序需要前一批的实测结果 |
| 直接抬高 `MAX_CLAIM_GRAPH_NODES` | 那是投影 v2 格式契约变更，需要迁移；先解决"样本被当全图" |
| 合并 70 MCP 工具 / 43 CLI / 19 skills 入口面 | 这是产品表面与使用习惯决策，不是架构缺陷；README 已有入口对照表 |
| 清理 `mutation_outbox` 历史 | 涉及既有保留策略与审计契约，需单独授权 |
| 引入 mypy 全量类型检查 | 注解风格统一会产生数百条告警，属独立工程，会淹没本计划信号 |
| 修复 `auto_ingest_runners` inert seam 与 `native_llm` 死代码 | 是结果不是原因；P3 断环后自然归位，提前动会制造新的反向边 |

---

## 9. 首个可执行的动作

P0.1（2 个文件，零风险，可立即执行）：

```
README.md:47    INGEST_CONTRACT_VERSION = 5   →   = 6
schema.md:1     Schema V8.0                   →   与 _SCHEMA_VERSION = 9 对齐
schema.md:159   同一处版本串
```

随后 P0.2 用测试把这两个数字钉住，使 P1 的迁移依据不再漂移。

---

## 附：本计划所依赖的实测数据点（可复现）

```sql
-- 规范表约束缺失
SELECT PRAGMA foreign_keys;                              -- 0
-- entities 双权威
SELECT count(*) FROM entities WHERE ttl IS NOT NULL;      -- 5 / 7905
-- 图边漂移
SELECT count(*) FROM claim_graph_edges;                   -- 10293
SELECT count(*) FROM page_graph_edges;                    -- 10400
-- 投影截断
-- ~/MEMORY/wiki/projection_pair_manifest.json → counts.claim_nodes 2500
--                                               canonical.claims 321520
```
