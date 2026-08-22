# GBrain 借鉴与 Vector Lake 历史负担修复方案

基线：`origin/main` `fc5ee4bd53e0936add5023e98a623921e606ad2c`，Vector Lake `11.16.0`。

## 1. 问题定义

Vector Lake 已经具备治理、证据和可恢复写入优势，但存在四个产品化缺口：检索质量缺少可复现实验、标题/别名命中依赖 FTS 候选、Agent 面对完整 MCP 工具墙、运行契约对 operational memory 的“物理存储”和“逻辑权威”表述不完全一致。

目标不是复制 GBrain，而是在不改变 SQLite canonical、Claim/Evidence 边界、payload sandbox 和 preview/apply 约束的前提下，借鉴四项机制。

## 2. 范围

范围内：

- `GBV-01`：只读、数据集哈希绑定的检索 benchmark runner。
- `GBV-02`：key、id、title、alias 的确定性精确召回通道。
- `GBV-03`：Vector Lake 原生 Agent-memory verb contract。
- `GBV-04`：可选、精确 allow-list、fail-closed 的 MCP memory surface。
- `VLD-01`：澄清 SQLite canonical、Markdown 投影与 operational-memory read model。
- `VLD-02`：把“有评估记录表但没有可执行检索评估器”的历史缺口闭环。
- `VLD-03`：把“51-tool 默认墙”变为默认兼容、可选薄表面。

范围外：

- 不部署或切换当前 RC。
- 不修改 `C:/Users/shich/.codex/config.toml` 或清理旧 marketplace。
- 不触发 Vector Lake 同步、GC、嵌入、治理或知识写入。
- 不实现 Agent 直接 `forget`。遗忘必须走受治理的 Claim/Source 生命周期，避免绕过审计历史和 CBSS AcceptedFact 边界。
- 不承诺与 GBrain MEMORY_VERBS 协议兼容。

## 3. 方案比较

| 方案 | 机制 | 主要风险 | 结论 |
|---|---|---|---|
| A. 完整复制 GBrain verbs 与自动化 | 引入相同七 verbs、自动 forget、远程服务和自治循环 | 双重事实源、治理降级、写入语义冲突 | 拒绝 |
| B. Vector Lake 原生兼容式增强 | 复用现有 canonical/search/query，新增稳定适配器和可选薄表面 | 新契约需要长期兼容 | 采用 |
| C. 只增加文档与 benchmark | 不增加 Agent API | 无法降低工具墙和跨 Agent 接入成本 | 不足 |

## 4. 设计决策与验收

### GBV-01 可复现检索基准

- 输入遵循 `vector-lake-retrieval-benchmark/v1`。
- 报告绑定数据集 SHA-256、evaluator version、top-k 和 embedding 策略。
- 默认关闭远程 query embedding，避免网络、模型版本和费用造成隐性漂移。
- 输出 P@K、R@K、MRR、nDCG@K、逐查询排名和阈值失败项。
- 默认只读，不自动写 `quality_evaluation_runs`。

验收：相同数据集和排名产生相同报告；重复 query ID、非法阈值和非法 top-k 失败关闭；远程 embedding 环境在运行后恢复。

### GBV-02 标题/别名精确召回

- 使用 NFKC、空白折叠和 casefold，只处理 identity equality，不扩大为模糊匹配。
- 从完整 node 集生成 generation-scoped lookup，保留同一 alias 的所有声明者。
- 精确命中在 FTS、向量和 PPR 之前进入候选，并跳过远程 query embedding。
- 精确 Source 在 `top_k=1` 时不被来源配额误删。

验收：key/id/title/alias 可命中；全角/半角一致；歧义 alias 返回所有候选；projection generation 变化后缓存失效。

### GBV-03 Agent-memory contract

稳定 verbs：`recall`、`remember`、`entity`、`synthesize`、`context_pack`、`delta`。

- `remember` 继续使用 sandboxed payload 和 Mutation Coordinator。
- `synthesize` 固定为 proposal-only dry-run。
- `entity` 只做精确身份解析并显式返回歧义。
- `delta` 只返回当前页面投影更新，不声称包含删除历史。
- `forget` 明确不提供，避免把派生 memory row 当成可直接删除的事实源。

验收：manifest 明确每个 verb 的可变性、限制和遗漏；所有响应携带 contract version。

### GBV-04 MCP 薄表面

- 默认 `full` 保持向后兼容。
- `VECTOR_LAKE_MCP_SURFACE=memory` 只暴露 runtime status、capabilities 和六个 verbs。
- 未知 surface、缺失必需工具或过滤后集合不完全相等时启动失败。

验收：多余工具不可 list、不可 dispatch；默认 full 不缩减现有工具。

## 5. 历史负担与停止条件

| 负担 | 本次动作 | 停止条件/残余 |
|---|---|---|
| operational memory 的物理位置与逻辑权威混淆 | 对齐 README、CONTEXT、schema | SQLite 表仍是 claims 编译的 read model，不是独立事实源 |
| 评估账本没有执行器 | 增加只读 benchmark 和数据集契约 | 未提供真实生产 golden dataset，不伪造质量基线 |
| 默认 MCP 工具墙 | 增加 opt-in memory surface | 默认 full 保持兼容；不自动改部署环境 |
| 旧 marketplace 导致 `codex plugin list` 失败 | 记录为外部运维债务 | 修改全局配置或删除旧 snapshot 需要单独授权 |
| 大型单体模块 | 新能力放入独立模块，避免继续堆入 store/search | 本次不做无关的大规模重构 |

若精确 identity lookup 导致不可接受的内存增长、薄表面需要绕过 payload sandbox、或 benchmark 需要隐式调用远程服务，应停止上线并回退对应功能。
