# 提案：存储/域层的职责越界（分层依据错误）

- **状态**: 待评审（**未实现任何改动**）
- **日期**: 2026-09-15
- **范围**: P3.2 剩余 12 条反向边中的 7 条
- **依据**: 只读调用点分析（`vector_lake/` 源码 AST + 精确行号）

---

## 1. 问题陈述

P3.2 剩下 12 条反向边里，7 条集中在同一形态：**存储层与域层依赖上层**。

```
storage  governance_store -> governance_metrics      （6 个调用点）
storage  governance_store -> provenance_retention    （1，写事务内）
storage  governance_store -> claim_extractor         （9 个调用点）
domain   provenance       -> governance_metrics      （1）
domain   schema_validator -> indexer                 （1）
domain   provenance_retention -> tool_claim_provenance
derived  runtime_health   -> tool_auto_ingest
```

**我原先把它概括为"存储/域层越权触发"——这个概括只对了一半。** 逐调用点查证后，它其实是**四个方向不同的错位**，其中两个是**反方向**的（函数该在更低层，却住在更高层）。

---

## 2. 根因：分层依据是"谁读数据"，不是"谁担责任"

证据来自函数**被归档的位置**与它**实际做的决定**之间的偏离：

| 函数 | 被放在 | 实际在做什么 | 应该在哪 |
|---|---|---|---|
| `retain_current_reviewed_provenance` | domain | 接收 `conn`，在**规范写事务内**改规范行 | storage |
| `build_claim_graph_projection` 等 5 个 | storage | **派生/投影/分析**（需要派生层注解） | derived |
| `validate_schema` | domain | 需要**投影事实**（合法链接目标集），故导入 indexer | domain + 依赖注入 |
| `extract_page_objects` / `classify_non_claim_text` 的 9 个调用点 | storage 调 domain | 内容→对象的**领域抽取**、污染**分类判定** | 取决于调用方归属 |

共同特征：**函数按"它读什么数据"归档，而不是按"它拥有什么决定"归档。** 读规范行 ⇒ 放进存储层；需要投影 ⇒ 导入投影层。层边界因此被数据访问路径而非责任边界决定。

---

## 3. 四个子问题（证据、规模、正确归属）

### A. 规范行操作住在域模块（275 行，清 2 条边）

**证据**：`governance_store.py:7128`，位于 `_apply_change_sets_batch_unchecked` —— **规范写事务内部**：

```python
# A re-extraction proposal is the base. Preserve only CURRENT, unchanged,
# operator-reviewed official bindings whose complete producer proof can be
# reverified now; fail closed before version append or page-scoped delete.
from vector_lake.provenance_retention import retain_current_reviewed_provenance
proposed_claims, proposed_evidence = retain_current_reviewed_provenance(
    conn, old_claims=..., old_evidence=...,
)
```

**关键约束（决定修法）**：它接收 `conn`、操作规范行、且注释明确要求"在版本追加或按页删除**之前** fail closed" ⇒ 它**必须留在那个事务里**。所以"把调用反转上去"是错的（会破坏原子性）。

**正确归属**：它是**存储操作**，只是住在了域模块。下移到 storage。
**附带效果**：`provenance_retention → tool_claim_provenance` 同步消解（生产者证明的取数助手随之下移）。
**规模**：275 行 + `provenance_retention` 内相关助手；波及面小（消费者仅写路径 1 处 + 测试）。

### B. 派生/分析函数住在规范库（377 行，清 6 条边）

**证据**：五个函数调用 `governance_metrics.annotate_claim_validity` / `find_merge_candidate_report`：

| 函数 | 行范围 | 行数 | 性质 |
|---|---|---|---|
| `_validate_operational_memory_delta_scope` | 2582-2620 | 39 | 读模型校验 |
| `_refresh_operational_memory_delta` | 2623-2705 | 83 | 读模型刷新 |
| `annotated_claims` | 2772-2778 | 7 | 读模型 |
| `build_claim_graph_projection` | 5048-5236 | 189 | 投影构建 |
| `create_merge_suggestions` | 5239-5297 | 59 | 合并分析 |

外加 `provenance.build_trace_for_query`（溯源报告）。

它们**都读规范行**，所以被放进存储层；但**没有一个拥有规范状态** —— 全部是派生、投影或报告。

**正确归属**：上移到 derived。这就是 `tool_ingest` 引擎拆分（批次 11）的**镜像操作**：同样是"实现住错层"，方向相反。
**规模**：377 行上移；**波及面较大** —— `build_claim_graph_projection` 在 `governance_store` 之外有 **16 处引用**，`create_merge_suggestions` 2 处，其余 1-4 处。合计约 25 处需重定向。

### C. 校验器导入投影层（1 条边，17 个调用点）

**证据**：`schema_validator.py:244-258`，在 `validate_schema` 内部：

```python
# Schema-v9 index.json is a static locator, not the projection payload. Use the
# committed reader so a stale/tampered v2 binding fails closed instead of
# silently validating the locator as an empty legacy index.
from vector_lake.indexer import read_committed_index_snapshot
index_data = read_committed_index_snapshot(index_path)
entities_in_index = { ... titles + aliases ... }
```

**为什么它存在**：校验"页面里的 `[[链接]]` 目标是否存在"需要**投影事实**（合法标题/别名集合）。这个需求本身合理；问题是它用**导入**去取，而不是**接收**。

**正确归属**：校验策略留在 domain，**事实注入**。调用方传入 allowed-target 集合。
**必须保留的性质**：v9 的 fail-closed 绑定 —— 调用方仍须用 `read_committed_index_snapshot` 读（不能退化成读 locator）。
**规模**：函数体改动小；但 `validate_schema` 有 **17 个调用点、跨 10 个模块**，签名变化面广。
**明确禁止**：用"默认参数 = 不校验链接目标"来减小波及面 —— 那会静默削弱校验，等于用退化解换指标。

### D. 存储层内的领域抽取与分类（9 个调用点）

**证据**：

| 调用 | 位置 | 性质 |
|---|---|---|
| `extract_page_objects` ×1 | `canonical_page_version_from_content` | 由内容**重抽取**对象以算页版本 |
| `extract_page_objects` ×3 | `create_change_set` / `prepare_change_set_from_content` / `migrate_existing_wiki` | 变更集准备 |
| `classify_non_claim_text` ×5 | `rebuild_operational_memory` / `_legacy_operational_memory_views` / `search_operational_memory_views` / `_scoped_pollution_records` / `remediate_operational_memory_pollution` | 污染过滤（读模型路径） |

两个子形态，**不能一起处理**：
- **分类判定**（5 处）：若 `classify_non_claim_text` 是纯谓词，下移到 base 即可（最便宜的一条）。
- **内容重抽取**（4 处）：`canonical_page_version_from_content` 由内容重抽取对象来推导**规范页版本** —— 这是"规范状态的**定义**依赖领域抽取"，不是越权，而是**规范模型本身的耦合**。这条需要独立设计（例如把页版本改成由调用方显式提供对象集），不能当搬运动作。

---

## 4. 提议的所有权规则

> **一个函数属于"拥有其决定"的那一层，而不是"拥有其输入"的那一层。**

派生判据：

| 该函数… | 归属 |
|---|---|
| 在事务内改规范行 | **storage** |
| 由规范行**推导**出新值/视图/报告 | **derived** |
| 决定"什么算合法"（策略），事实由外部提供 | **domain**（依赖注入） |
| 决定"何时触发" | **orchestration** |

对本次 7 条边的映射：A 是"事务内改规范行却住在 domain"；B 是"由规范行推导却住在 storage"；C 是"策略该注入事实却直接导入"；D 混合。

---

## 5. 分阶段方案

> **✅ P-A 已执行（`c10c98a`）**：边 12 → 10，**SCC 33 → 13**（−20，与模拟完全一致）。
> **但它不是原计划里的“275 行搬迁”——它是重分类。** 前置核查发现 `provenance_retention` **整个模块就是那一个簇**（27 个名字覆盖 577 行中的 16-577，簇外为空），所以只需把它的依赖降到目标层以下，然后重分类即可。两项使能改动：
> 1. **持久标识符助手被重复实现**：`claim_extractor._stable_id` 与 `governance_store._stable_id` **逐字节相同**（BLAKE2b/12），现统一为 base 的 `wiki_utils.stable_short_id`，两处改为委托（零值变化）。
> 2. **`evidence_foundation` 只依赖 wiki_utils ⇒ base 合法**，已重分类；并从 `tool_claim_provenance` 接管 provenance-repair 身份（`PROVENANCE_REPAIR_EXTRACTOR_NAME`/`_VERSION`/`claim_page_key`）。
>
> **本次避开的陷阱（值得单独记）**：batch 1 引入的 `wiki_utils.stable_identity_digest` **形状相同**（`prefix_` + 24 个 hex）**但用 SHA-256**，与 BLAKE2b 族**值不同**。若图省事复用它会**静默重写全部已持久化的** entity_id / evidence_id / source_id / 幂等键。两个函数现在都带 docstring 声明“**不可互换**”及各自属于哪个族。

### 5.0 先做了一次环影响模拟（读图，非执行）

在写方案前，我在导入图上模拟了每个子问题“边消失”后的效果：

| 模拟 | 边数 | 最大 SCC |
|---|---|---|
| 现状 | 12 | **33** |
| **P-A** 消灭 `governance_store → provenance_retention` | 11 | **13** ← 降 20 |
| P-C 消灭 `schema_validator → indexer` | 11 | 29 |
| P-B 消灭 `governance_store/provenance → governance_metrics` | 10 | 33（无变化） |
| P-D 消灭 `governance_store → claim_extractor` | 11 | 33（无变化） |
| **四项合计** | **7** | **7** |

**这一下改变了优先级。** `governance_store` 是 `provenance_retention` 的**唯一生产导入者**（其余两处是测试），而环路是：

```
governance_store → provenance_retention → tool_claim_provenance
                 → {db_store, governance_store, governance_metrics, claim_extractor, tool_projection, ...}
                 → … → governance_store
```

即存储核心进入那个子图的**唯一入口**就是这一条边。切断它，`provenance_retention` + `tool_claim_provenance` + 仅经由它们可达的模块全部离开 SCC ⇒ **33 → 13**。

**同时必须说清楚**：**P-B（377 行、25 处重定向）和 P-D 对环毫无影响**（均保持 33）。它们的正当性只有“层序干净 / 不再跨层”，**不能**用“缩小环”来为它们的成本辩护。这个区别应当写进评审依据——否则会用一个大重构换来一个看起来更大、实质无变化的指标改善。

### 5.1 批次

| 阶段 | 内容 | 状态 | 清边 | **SCC** |
|---|---|---|---|---|
| **P-A** | `retain_current_reviewed_provenance` → storage（实为重分类） | ✅ `c10c98a` | 2 | **33→13** |
| **P-D1** | `classify_non_claim_text` → 新 base `non_claim_text` | ✅ `8b6481b` | 0 | 13（无变化） |
| **P-C** | `validate_schema` 改为注入可调用对象 | ✅ `8b6481b` | 1 | **13→8** |
| P-B | 5 个派生函数 + `build_trace_for_query` 上移聚合模块 | 待做（你已确认方案） | 2 | 预计无变化 |
| P-D2 | `canonical_page_version_from_content` 的抽取耦合 | ⚠️ **需重新决策**（见下） | 1 | — |

#### P-C 的实测要点

**先量再改，于是改动很小**：标签碰撞检查以可选参数 `index_path` 为条件，而 **16 个调用点中只有 2 个传它**（另有 6 个经由 `verify_asset`），其余根本不进那段代码。改为传回调后：
- `schema_validator` **零向上依赖** ✓
- 读取逻辑搬到 `indexer.committed_index_entities`，保留“committed reader + fail-closed”语义与惰性读取
- **顺手堵掉一个我自己引入的漏洞**：那段代码外面包着会吞 `TypeError` 的 `except`，所以调用方若错传一个 `Path` 而非可调用对象，会**静默跳过校验**。现改为**非可调用即报错**，不静默。

#### P-D2 需重新决策（我先前对它的描述不准确）

`canonical_page_version_from_content` 有 **21 个外部调用点**（生产 11 处跨 5 个模块）。你已确认“接受页版本由调用方显式提供对象集”的**原则**，但落地后发现：那会把“由内容抽取对象”的负担平摊给 **21 个调用方**，而它们大多并不持有对象集，仍需 `extract_page_objects` ⇒ **耦合只是上移，并未消失**。

因此 P-D2 存在三个选项，需你选：
1. **维持现状**（`governance_store → claim_extractor` 保留为已知的 1 条边）；
2. **在 `governance_store` 内部保留一个薄适配层**，把“内容→对象”作为**注入到 storage 的可调用对象**（形状同 P-C），这样 storage 不导入 domain，但需在组合根接线；
3. **真正拆分**：把页版本的定义搬到 domain，由 storage 调用 domain —— 但这与“storage 不得依赖 domain”直接矛盾，除非把页版本计算归为**派生**。

我倾向选项 2（与 P-C 同形，一致性最好），但它的第一个动作需先在组合根找到一个接线点，不在本提案范围内。


**建议顺序**：**P-A → P-D1 → P-C → P-B → P-D2**。

理由：**P-A 的收益/成本比远远优于其余全部**（275 行、波及极小、单项就能把环降 20），应第一个做；P-D1 便宜且为 D 铺路；P-C 虽然只清 1 条但签名波及 17 处，适合在状态干净时做；**P-B 放最后**——它是唯一“无环收益”的大工程，应该在明确知道只为层序、不为环之后再做。

**预期结果**：四项全做 ⇒ 边 12→7，SCC 33→7。剩余 5 条见下。

---

## 6. 风险与开放问题

**已知陷阱（不要再踩）**：
- `mcp_server`↔`tool_doctor` 与 `db_store`↔`native_llm` **互相依赖**：改层只会同层安置，环不变。这两条必须"改调用方向"或"打断环"，不能重分类。**区间测试不够，须同时看环。**
- 一个符号可能被两个模块绑定，并因"哪个模块的代码在调用"而解析到不同对象 ⇒ 打补丁/改导入时只改一处会**静默失败**（批次 11 实测：测试不报错，只是断言"调用次数为 0"）。

**开放问题（需业主决定）**：
1. **P-B 上移的新家**：一个聚合模块（如 `derived/projections.py`）还是各自就近？聚合会新增一个被广泛导入的枢纽，可能重演 `wiki_utils` 的 fan-in 问题。
2. **P-C 的注入形状**：传集合、传可调用对象、还是传一个窄接口？传可调用对象保留了"惰性读取 + fail-closed"语义，但引入隐式依赖。
3. **P-D2 的规范语义**：`canonical_page_version_from_content` 由内容重抽取对象 —— 是否接受"页版本由调用方显式提供对象集"？这会改变规范页版本的**定义**，属于 L3 语义变更，需要独立评审。
4. **是否值得**：P-B 的 377 行上移 + 25 处重定向，换来 6 条边且**不保证**缩小 SCC（上移可能把函数带进别的环）。若目标只是"层序干净"，收益明确；若期望环变小，需要先模拟。

---

## 7. 不做什么

- **不用"反转 7 次调用"逐条处理。** 这 7 条形态不同（A 该下移、B 该上移、C 该注入），逐条反转会把其中至少两条改成错误方向。
- **不用默认参数削弱 `validate_schema`。** 见 §3.C。
- **不在同一批里混做 A 和 B。** A 是向下、触及事务；B 是向上、波及 25 处。混做会让回滚单位过大。
- **不把 `canonical_page_version_from_content` 的抽取耦合当搬运动作。** 它是规范模型的语义问题。
