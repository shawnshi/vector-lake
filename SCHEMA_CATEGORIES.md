# Vector Lake Schema Categories (受控词表)

This document defines the rigid ontology for the `categories` field in Vector Lake V7.0.
All entities, concepts, and synthesis logic nodes must belong to one of these macro-domains. 
Folksonomy and fine-grained labels should go into the `tags` field instead.

## Allowed Categories
- `Uncategorized`: Default category for imported legacy nodes ONLY. **Do not use for new nodes.**
- `Artificial_Intelligence`: AI models, architectures, AI/Agent methodology, training algorithms (e.g. LLM, RAG, RL, Agent Architecture).
- `Healthcare_IT`: Digital health systems, hospital implementations, electronic health records (e.g. EHR, HIS, Epic).
- `Strategy_and_Business`: Market analysis, corporate strategy, go-to-market, market intelligence, business strategy.
- `System_Architecture`: Software engineering, distributed networks, technical systems, data architecture, development methodology.
- `Philosophy_and_Cognitive`: Knowledge management, epistemology, human-computer interaction, dialectics.
- `Biomedicine`: Clinical science, pharmacology, molecular biology.
- `Policy_and_Governance`: Policy analysis, regulations, industry guidelines, and compliance frameworks.
- `Entities_and_Actors`: Real-world actors including organizations, people, researchers, companies, and industry ecosystems.

## Enforcement
Agents should generally use the above categories. However, if a concept fundamentally falls outside these bounds and warrants a new domain, Agents **may** propose new categories using the `propose_schema_mutation` MCP tool. 
Proposed categories will be logged in the governance queue for review. Once approved, this document will be automatically updated.

## Domain Facet (主题/行业 facet，两层登记制)

`categories` 是宏观受控轴；`domain` 是**主题/行业 facet**，它**开放但需登记**——因为它被使用的方式就是"这条记录讲的是哪个主题/行业"，而不是第二个宏观轴。2026-09-23 实测：被排除在这 9 个宏观值之外的 118 个页面不是脏数据，而是 54 个纵向主题（`Sociology`、`Startup`、`Venture_Capital`、`Semiconductor`、`Defense_Tech`、`Tobacco`…），它们的 `categories` 早已写明了宏观归属（68 页是 `System_Architecture`），而纵向信息在 118 页里只有 1 页的 tags 留存。**把它们抹平进 9 个宏观值等于删掉唯一记录该主题的字段。**

### 第一层：宏观域（9 个，语料主体在用）

- `Medical_IT` — 医疗信息化：产品、项目、政策落地、厂商
- `Artificial_Intelligence` — 大模型、Agent、训练与推理方法
- `System_Architecture` — 软件与分布式系统、数据架构、工程方法
- `Enterprise_Software` — 企业软件与平台产品、交付与运维
- `Strategy_and_Business` — 战略、市场、投资、商业模式
- `Policy_and_Governance` — 政策、监管、合规
- `Biomedicine` — 临床与基础医学
- `Cognitive_Science` — 认知、知识管理、哲学
- `General` — 尚未收窄。不是兜底垃圾桶：能收窄就不要用

### 第二层：已登记纵向（`DOMAIN_VERTICALS`）

登记条件是"它命名了一个主题/行业，且没有任何宏观域能忠实表达它"。未登记的值：新节点**拒写**，存量页在 lint “15. Domain Vocabulary” 报告——登记是一次治理动作，不是因为某个页面存在就自动获得。

| 纵向 | 为什么 9 个宏观值不够 |
|---|---|
| `Sociology`、`Academic_Sociology` | 社会科学，既不属 `Cognitive_Science`（认知/知识管理）也不属 `Policy_and_Governance` |
| `Neuroscience` | 神经科学，与 `Biomedicine` 的临床取向不同 |
| `Science`、`Scientific_Research`、`Science_Epistemology` | 科学本体与科研活动，无宏观域对应 |
| `Mathematics`、`History`、`Arts`、`Narratology`、`Communication` | 基础学科与人文学科 |
| `Study` | 语言/学习素材（现存 1 页：`Concept_American-Idioms`） |
| `Media`、`New_Media` | 媒介作为行业与学科，不是"企业软件"也不是"战略" |
| `Tobacco`、`Space_Technology`、`Agriculture_Machinery`、`Consumer_Electronics` | 行业纵向，无宏观归属 |

（原值 `Media_IT`、`Manufacturing_IT`、`Financial_IT` 不在登记表内：它们是**行业里的 IT**，归 `Enterprise_Software`；`Robotics`、`AGI` 归 `Artificial_Intelligence`；`HCI` 归 `Cognitive_Science`；`中医药` 归 `Biomedicine`。）

#### 别名登记（`DOMAIN_ALIASES`，2026-09-26）

与纵向不同，**别名**指“该 subject 已被某个宏观域忠实命名，只是写法不同”。登记后该写法被接受，且检索按规范化值匹配；它**不是**第三个域值，所以既不进入上面 9 个宏观值，也不进入纵向表。

| 别名 | 规范化到 | 为什么是别名而不是纵向 |
|---|---|---|
| `Healthcare_IT` | `Medical_IT` | `Healthcare_IT` 是 `categories` 轴对同一 subject 的写法（本文件第 10 行：Digital health systems, hospital implementations, EHR/HIS/Epic），`domain` 轴已有 `Medical_IT` 忠实表达它。登记为纵向会让同一个 subject 在 facets 里有两个值，而 `tool_search._passes_filters` 是按值相等判定命中的。 |

与上一条排除表的区别：`Media_IT` / `Manufacturing_IT` / `Financial_IT` 所在行业没有同名宏观域，所以它们归 `Enterprise_Software`；`Healthcare_IT` 所在行业有（`Medical_IT`），所以它是别名。

触发来源：2026-09-26 一批四人（任连仲/刘海一/李包罗/薛万国）源文件中，三条随机写了对 `Medical_IT`，刘海一那条写了 `Healthcare_IT`，被 `domain is neither a macro domain nor a registered vertical` 确定性拒写并弃置（`ingest_abandoned_sources`）。两侧 `categories` 与 `domain` 用不同词汇表称呼同一个主题，才是真正的触发因。

### 迁移现状（2026-09-23）

存量曾携带 **192 个不同 domain 值**，其中 67 个只用在 1 个页面上，近义并存是明摆着的（同一个主题有 `AI_Industry` / `AI_Research` / `General_AI` / `AI_Architecture` / `AI` / `Artificial_Intelligence` 六个值）。已完成：宏观值收敛 2 086 页 + 第二批 69 页（IT/商业/地缘/医疗类的纵向，有忠实宏观归属）；登记的纵向值保留原值不动；`auto-stub` 占位页与生成物不参与域报告（前者还没有主题，后者是 wiki 自己生成的索引）。

**【强制】入湖收容协议 (Ingest Protocol):** 
在任何执行知识入湖 (Ingest) 或合并的环节，**绝对禁止使用 `Uncategorized`** 以及任何自创分类。如果遇到边缘概念，必须向现有的 9 大宏观分类进行降维对齐，或者触发 `propose_schema_mutation` 工具报警。
