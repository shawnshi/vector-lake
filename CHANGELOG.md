# Unreleased

## 声明只在唯一时可用，且文件名优先于声明（+ 存根 id 从页面名派生）

上一批把「一条链接指向哪个页面」统一成了核心名规则，这一批收掉剩下的三个小项，并在复核后修掉四个缺陷。

### A3：标题/别名是**声明**，声明会与文件名和彼此冲突

原实现用**平铺赋值**把标题与别名写进链接表，于是有两类错误：

1. **两个页面声明同一个名字**时，链接解析到 `os.listdir` 顺序里的最后一个 —— 掷硬币，而且报告把它当事实；
2. **声明与某个页面的文件名相同**时，声明**覆盖了那个页面自己的 stem**，于是一条指向真实文件的链接被另一个页面接走。

现在：声明在解析循环里先收集，循环之后**只加入「恰好一个页面声明」的名字**，且用 `setdefault`，**文件名永远优先**。
有争议的名字保持未解析；破链文案会点名声明它的页面（与「有争议的核心名」同一套措辞）。实测该规则在活库上
**本来就应该有影响**（我第一版测量脚本查错了字段，误判为「零影响」，见下）：11,723 个不同声明名中有
**272 个被两个以上页面声明**，**35 个声明与另一个页面的文件名相同**。

### 复核判 BLOCK，四条 P1 都是真的，已修

- **P1-1（我自己的守卫被拼写绕过）**：声明用**原始拼写**查表，于是 `title: Atrium Health` 与
  `[[Atrium-Health]]` 这种（`_`/`-`/空格 是同一个名字）既被误报成「target does not exist」，又**绕过自动修复守卫**
  → lint 会为一个活库上真实有争议的名字写出第三页。修法：消息与守卫改用**归一化**的声明计数表。
- **P1-2（规则只活在调用方之一）**：`tool_query` 也写存根，而它只看 `covering_page`（看不见声明），
  于是它对同一个名字**照样写出第三页**；而且对「链接由标题回答」的普通情形（`[[Epic]]` 对
  `Vendor_Epic-Systems`）也会造页。修法：把规则移进所有者 —— `create_stub(..., contested=...)` 拒写，
  两个调用方传同一个集合（规则函数 `stub_creator.contested_names` 是唯一实现）。同时修掉
  `covering_page` 的**原始核心名比较**（`Vendor_Foo_Bar.md` 没能拦住 `[[Foo-Bar]]` 的存根）。
- **P1-3（同一份报告自相矛盾）**：检查 3 的别名自动修复会把「落选声明」从磁盘上删掉，而检查 7 仍按修复前的
  快照说「两个页面声明了它」。修法：声明计数表随删除同步更新，`contested` 集合在检查 3 **之后**再计算。
- **P1-4（头号规则没有能失败的测试）**：`test_a_declaration_does_not_displace_a_filename` 在旧行为下**照样通过**
  （链接两种情况都不算破链，只是归属不同）。改为断言**归属**：该文件不得被报为孤儿。
- 另修 P2：`core_name_maps` docstring 仍承诺一个已被撤掉的「重建」（见 A2）、改名时未把**新文件名**注册为键、
  `tool_lint._generate_id` 是 `stub_creator.generate_id` 的**随机孪生**（同一个问题两个答案，现统一委派，
  顺带让「重复 id 修复」幂等）、以及上一批那条**空洞断言**（`"[[BadName]]" not in report` 在本就行不通的构造下恒真）。

### A2：撤销 —— 我构造不出一条能观察到差异的路径

我原以为「改名后核心名映射过期」是个缺陷并加了重建；实测**没有任何可观察效果**：改名会重写 `[[旧名]]` 与
`[[旧名|别名]]`，并把旧核心名加入被改名页面的 aliases，因此残留的旧名拼写仍能经别名路径解析。复核补了一个更强
的理由：**改名保持核心名不变**，所以 `core_pages` 的基数与 `unique_cores` 的成员资格在改名前后是不变量。
已撤销该重建，只留下 `core_name_maps` 这个纯提取。

### A4：存根 id 从页面名派生

原来 `id` 是随机抽取：它不查任何东西（同秒批量创建有碰撞可能，且由 lint 的重复 id 检查事后发现），重复创建同一
存根每次都得到不同 id。现在 6 位后缀取自页面名 SHA-1 的截断（形状不变，`20260918_ab12cd`），于是同日重复创建**幂等**，
两个调用方产出的页面**逐字节相同**（含 id）。残余风险写在 docstring 里：6 位 base36 的两个不同名字理论上仍可能碰撞，
那是 lint 既有重复 id 检查的覆盖范围。

### 活库实测（同一命令，HEAD 工作树 vs 工作副本，逐节对比）

| 节 | HEAD | 现在 | Δ |
|---|---|---|---|
| 7. Broken Links | 695 | 724 | **+29** |
| 8. Orphan Pages | 842 | 815 | **−27** |
| 11. Semantic Garbage Collection | 5 | 2 | **−3** |
| Issues 总数 | 5221 | 5220 | **−1** |

`+29 − 27 − 3 = −1`，与总数**逐字吻合**（复核曾质疑「+29−27 应得 +2」，因为它看不到 GC 那一节）。
GC 的变化是**严格收敛**、不是扩张：HEAD 上三个 `Standard_*` 页面因 `Inbound: 0` 达到归档条件，现在因为它们
**获得了入链归因**而不再达标（两个 `System_*` 页面不变）—— 即复核担心的「新增可删除页面」没有发生，方向相反。

关于复核「59 与 29 对不上」这一问，我重新测量并说明：报告按**(文件, 目标) 去重**计数，我此前的探针按**原始匹配**计数。
对「有争议且不是任何文件名」的 266 个名字：原始匹配 120 次 → 去重后 53 对 → 报告显示 +29；其余经由**核心名路径**
解析成功（有争议的*声明*不进表，但唯一的核心名仍会回答它 —— 这是刻意的优先序：文件名或核心名优先于声明）。
上一批引用的「59」来自参数不同的探针，已被此处的分解取代。

### 测试

`tests/test_lint_link_resolution.py` 新增 6 例（争议名无论拼写都被拒、query 侧同样拒、query 空路径返回二元组、
核心名归一化比较、争议别名与自动修复不在同一份报告里自相矛盾、改名后旧核心名仍可解析），并把两处**无法失败**的
断言改成可失败的。全量 pytest **809 passed**。可失败性逐项实测后恢复：改回原始拼写查表 → 恰好 2 例失败；
撤掉所有者侧拒写 → 恰好 2 例失败；核心名改回原始比较 → 1 例失败；不更新声明计数表 → 1 例失败；
query 空路径改回裸 `0` → 1 例失败；存根 id 改回随机 → 1 例失败。

## lint 的链接解析改为与存根创建器同一套规则

此前「一条链接算不算破」只看文件名/标题/别名，而存根创建器回答同一个问题时用的是**核心名**（`covering_page`）。
两半口径不一致，于是 lint 会把**已经有页面回答**的链接报成破链，而它随后写出的存根又不能让链接解析成功
（上一批的分裂守卫会拒掉那次写入）—— 这类链接在 lint 里**无法被修好**。

现在解析统一走 `resolve_link_target(target, link_target_map, unique_cores)`：先按原样查文件名/标题/别名，
再按**核心名**查一张**只含无歧义核心名**的表。破链判定与 `inbound_count`（孤儿检测的上游）同时改用它，
一条链接在全流程里只对应一个页面。

`unique_cores` 由磁盘上**所有**页面构成，且只保留「核心名唯一」的那些：同名核心对应两个页面本身就是缺陷
（活库有 40 组），把这种链接解析到排序最前的那一页等于把它藏起来。

### 实测（同一条命令，改动前后）

```
Broken Links: 788 -> 722        Issues: 5314 -> 5248
Scanned: 7923 (不变)            Auto-fixed: 0 (不变)      Orphan Pages: 842 (不变)
```

活库计数与 `quick_check` 不变。被救回的典型是**写错前缀**的链接：`[[Concept_CoMET]]`（只有 `Product_CoMET`）、
`[[Concept_Art]]`（`Product_Art`）、`[[Concept_DICOM]]`（`Standard_DICOM`）、`[[刘宁]]`（`Person_刘宁`）。

### 预测先于改动，并抓出了我自己的实现错误

改动**之前**就用只读探针算出：182 个不同的破链目标里 24 个、共 67 次出现可以被核心名规则解析 → 预期
788 → 722。第一版实现跑出 **680**，比预测少了 42。原因是它把目标**去掉前缀**后去查**通用表**，于是也会命中
标题/别名 —— 这条路让一个页面通过「它只是声明过的名字」被命中，并且当某个标题恰好等于一个有歧义的核心名时，
它会把两个页面争抢的链接悄悄解析掉。换成只查 `unique_cores` 后与预测**逐字吻合（722）**。这个错误由一个
新测试钉住（`test_a_title_cannot_resolve_an_ambiguous_core_name`，把实现改回宽变体即失败）。

### 复核结论与随之修的内容

独立复核（只读子代理）判 **OK with notes**，无 P0/P1；但有一条**判定为不成立**（C6），而且它是对的：

- **C6（闭环对「需要净化的目标」不成立）**：`[[Foo Bar]]` 会写出 `Concept_Foo-Bar.md`，而目标是按字面比较的 ——
  存根标题是 `Foo-Bar`、核心名也是 `Foo-Bar`，于是原始目标谁都匹配不上：链接每轮都被报破链，而那个本该修好它的
  存根又让创建器**静默跳过**自己的写入 —— 恰好就是本批声称钉死的「每轮报破链且无法修好」。修法：核心名**两侧都
  归一化**（`_` 与 `-` 在 wiki 其余地方本就是同一个名字）。这同时把 `_`/`-` 这条轴并进了唯一性守卫：
  `Concept_Foo-Bar.md` 与 `Product_Foo_Bar.md` 现在算**一个有歧义的核心名**，而不是两个「各自唯一」的页面
  （复核指出按字面比较会让两种拼写各自解析到不同页面，正是守卫要防的隐藏重复）。
- **C2 备注 3（非节点页面被核心名解析）**：`unique_cores` 原本由 `all_keys` 构成，因此也包含 `System_*` 与
  非节点产物，`[[Roadmap]]` 会解析到 `System_Roadmap.md` —— 而 `indexer` 会把这类页面从图里删掉，`stub_creator`
  也正是因为这个原因拒绝**创建**它们。按同一条「不要把缺口藏起来」的原则，核心名查表现在排除这类页面；
  按原样拼写仍然能找到它们（与改动前一致）。
- **C5 措辞**：有歧义的核心名仍留在 `broken_links` 里（计数含义不变），但文案不再只写「target does not exist」
  —— 那既不真也不诊断。现在写明「N pages share that name: A, B」，让操作者知道这是重名而不是缺失。
- **C6 次要件**：同一轮内写入存根后，`unique_cores` 现在也跟着更新（此前只有 `all_keys`/`link_target_map` 更新，
  导致同一轮里后续链接看到的是过期映射）。改名路径上的同类更新**登记为下一批**（守护进程跑的是
  `auto_fix=False`，这是操作者路径）。
- **C1（部分不成立）**：本批只让 lint 与存根创建器统一了口径。**图层的 typed link 解析仍用旧规则**
  （`claim_extractor` 只取 typed link，`indexer` 的 `alias_map` 只看 key/title/alias，没有核心名这条路），所以
  这 66 条被救回的链接对 lint 安静了，对 indexer 仍被丢弃。这是本批**明知而付的代价**，属于另一批（要动图层与活库
  边权）。反向也不一致：indexer 的 alias_map 含节点 `id`，lint 不含 —— 即「按 id 链接」在图层能解析、在 lint 报破链。
  已登记。

### 归因（用工具自己的计数器，逐项撤掉一个改动各跑一次活库 lint）

| 配置 | Broken Links |
|---|---|
| 本批之前 | 788 |
| 严格核心名解析（未归一化，含非节点排除） | 722 |
| 加上两侧归一化 | **695** |
| 归一化保留、非节点排除撤掉 | 695（**完全相同**） |

即：归一化正好解释了 **27** 条（722 → 695）；非节点排除在活库上**没有数值影响**（说明此前没有任何活库链接
经核心名解析到 `System_*`/非节点页面）——它是与既有原则的预防性对齐，不是一次数值修复。

### 测试

新增 `tests/test_lint_link_resolution.py` **10 例**，并**各自标注它到底能区分什么**（本项目此前多次强调这一点）：

- 核心名链接不再被报破链 —— **本批的真实回归测试**；
- 这类链接不需要写任何存根 —— 属**不变量守卫**（上一批的分裂守卫已让它无论改动前后都通过），保留是因为
  「不报」与「不靠写页面来『修』」合起来才是这个报告的含义；
- 有歧义的核心名仍然保持破链 —— 区分的是**我否决的那个实现**（解析到排序最前的一页），不是改动前的行为；
- 标题不能解析有歧义的核心名 —— 同上，专门钉住上面那个被实测抓出的错误；
- 写出的存根**确实**让链接在下一轮解析成功 —— 这条要**同时**撤掉两条路才会失败（存根标题是核心名、以及本次
  解析），docstring 如实写明它是关于结果而不是关于哪一条路生效；
- 真正缺失的目标仍被报告、仍被自动修复 —— 覆盖面守卫。

另外 4 例是复核修复带来的：需净化的目标仍能解析（对 C6 的回归测试）、`_`/`-` 对唯一性守卫是同一个名字、
指向生成型页面的链接不被核心名解析、有歧义的核心名在报告里点名其候选页面。

全量 pytest **790 passed**。可失败性均实测后恢复：去掉核心名查表 → 第 1 例失败；解析有歧义核心名 → 歧义例
失败；改回通用表 → 歧义例与标题例失败；同时撤掉上一批的标题修复与本次解析 → 闭环例失败；撤掉两侧归一化 →
「需净化目标」与「`_`/`-` 同一名字」两例失败；撤掉非节点排除 → 生成型页面那例失败。

## 坏链存根只留一个所有者（此前 lint 会分裂实体，query 一个也建不出来）

`tool_lint --auto-fix` 与 `tool_query` 收尾时各自实现了一套「给坏链写存根」的代码，两边在**每一个**
决策上都不同：

| 决策 | `tool_lint` | `tool_query` |
|---|---|---|
| 无前缀目标的文件名 | `Concept_<target>.md` | `<target>.md` |
| `id` | 生成式，如 `20260918_0f9t54` | 目标名本身 |
| 已有同核心名但不同前缀的页面 | **不检查**：「`Vendor_Epic-Systems.md`」旁边会造出 `Concept_Epic-Systems.md` | 检查，不写 |
| 写入路径 | `write_markdown_file`（校验文件名、schema 与编译事实守卫） | `execute_mutation_plan` + 手拼 YAML 字符串 |

两个结果都是错的，而且都在新测试里**对着调用方**复现出来：

- `tool_lint` 把一个实体**分裂成两个节点**；
- `tool_query` 对无前缀目标写 `<target>.md`，而那是**非法节点文件名**（`validate_wiki_filename`
  要求类型前缀），于是写入被拒、异常被吞成一条 warning —— 实测它对无前缀目标**一个存根也建不出来**
  （有前缀的目标同样 0）。

### 修法：一个拥有者，逐项选定基线（不是折中混合）

新增 `vector_lake/stub_creator.py`：`stub_type`、`stub_page_name`、`existence_index`、
`covering_page`、`stub_frontmatter`、`stub_body`、`create_stub`。每个决策**只取一边**并写明理由：

- 文件名/类型/校验/`id` 取 `tool_lint` 那套（唯一能通过校验的一边；活库 id 是自由格式的稳定标识，
  抽样 60 页有 59 页与文件名不同，所以 `tool_query` 的 `id = target` 才是异类）；
- 「同核心名已被覆盖则不写」取 `tool_query` 那套（lint 缺的正是这条）；
- 写入走 `write_markdown_file`（校验文件名、schema、编译事实/证据时间线守卫），不用手拼 YAML；
- 生成型（`System_*`）拒写：两边本来就一致，`indexer` 跳过这类页面，写出来只会让检查通过而图里依旧空缺。

`tool_query` 的 `_node_core`、`_node_type`、`_covering_page` 随之删除（前两者本就分别是
`node_vocabulary.strip_prefix` 和一行默认值），两个调用方改为调用同一个 `create_stub`；
`test_broken_link_stub_guard.py` 与 `test_node_vocabulary.py` 改为指向新家。

### 活库影响：零，且可验证

- `python cli.py lint` 走改造后的模块，数字与改造前**逐项相同**：`Scanned: 7923 files | Issues: 5314
  | Auto-fixed: 0`、`2. Naming Compliance: [PASS]`、`7. Broken Links: [FAIL: 788]`、`11. Semantic
  Garbage Collection: [FAIL: 5]`、`12. Governance Debt: [FAIL: 1]`；活库计数不变。
- 守护进程的定时 lint 用的是 `auto_fix=False`，所以活库上不会有任何写入。
- 活库确实存在 40 个「同一核心名有多个页面」的情况，但其中**没有一页**带 lint 存根的特征
  （生成式 `id` 且 `sources: []`）→ **lint 的分裂从未在活库发生过**，本次不需要任何清理。

### 测试

新增 `tests/test_stub_creator.py` 8 例：无前缀目标获得前缀（这正是 query 之前建不出页面的原因）、
生成型拒写、同核心名阻断写入（分裂规则）、重复调用只写一次、成品通过 `validate_schema`、`id` 形状符合
活库惯例，以及对**两个调用方**各一例：lint 不再分裂已存在实体；两个调用方对同一条链接产出的页面
（除生成式 `id` 外）逐字相同。

计数对账（避免把新模块带来的连带计入误当成本批新增）：全量 **777 passed** vs HEAD 的 766，
差额 11 = 本批新增 8 + `test_static_scope.py` 增 2 + `test_portability.py` 增 1 —— 后两者按
`vector_lake/*.py` 参数化，新模块自动加入了「无未绑定名 / 无函数内导入 / 无机器相关绝对路径」三道守卫。

### 复核结论与随之修的内容

独立复核（只读子代理）判 **OK with notes**，无 P0/P1：C1–C6 全部成立（两个缺陷确实被修掉且由调用方级测试
盯住、`create_stub` 各条拒绝路径成立、调用方没有丢东西、写入路径是原来两条的严格超集、`id` 无消费方要求
等于文件名、新模块不引入环也没有第三条延迟上行导入）。第 7 问（是否该把「链接解析」也一起改）判为
「拆分合理、比改动前更安全」，并指出一个附带收益：旧 lint 对同一条破链会**无条件重写**已存在的
`Concept_Foo.md`，只因编译事实守卫恰好拦住才不会丢内容；`covering_page` 现在直接拒绝这次写入。

按复核意见修掉 4 条 P2，其中 **F1 是我在本批引入的回归**：

- **F1（回归）**：我把**页面 stem** 传给了 frontmatter/正文构造器，于是 `[[BrandNew-Thing]]` 得到的存根会写
  `title: Concept_BrandNew-Thing` 与 `# Concept_BrandNew-Thing`。合并前的 lint 写的是 `title: target`（= 裸名），
  所以这是我这边弄坏的。修法：`title`/H1 一律取**核心名**，自链接指向**真实存在的页面名**（`[[Concept_X]]`，
  今天就能解析）。活库的既有页面正是写裸名（如 `Vendor_Epic-Systems.md` 写 `title: Epic`）。
- **F2**：正文原来把日期写成 `[[2026-09-18]]` 链接 —— 今天是一条破链，而任何人跑一次 `lint --auto-fix` 就会
  给它造出一个 `Concept_2026-09-18.md` 垃圾页。实测证据：`validate_wiki_filename("Concept_2026-09-18.md")`
  **合法**，且活库**0** 个日期形页面；活库既有页面的写法是裸日期（`Last Reshaped: 2026-06-02)`）。已改为裸日期。
- **F4（便宜的一半）**：文件名净化比校验器窄——空格/括号/下划线会被拒，且用 `_` 替换也不可行
  （`Concept_Foo_Bar.md` 被「Strict Naming Violation」拒）。改为把校验器禁止的字符（含下划线）整段替换成
  单个 `-`：`Foo Bar`/`Foo_Bar`/`Foo(Bar)` → `Concept_Foo-Bar.md`，实测三者现在都合法。**两个调用方在
  归一化上的分歧**（query 归一化目标、lint 不归一化）属调用方语义，留给下一批。
- **F5**：query 侧原本又建了第二个索引，导致 `existing_files.add(written)` 是死写入。改为把扫描前那个索引
  传给 `create_stub`（它在内部就地更新），死写入删除。

**登记为下一批**（复核同意拆开，避免同时改动报告语义）：lint 的**链接解析**改为复用 `covering_page`（否则
它新建的 `Concept_Foo.md` 并不能让 `[[Foo]]` 解析成功），连同 F3（写入失败与「无需创建」目前返回同一个
`None`，系统性的闸门失败会被读成「没有需要创建的」）、F4 的归一化分歧、F6（生成式 `id` 未查重）以及
「`fixes_applied` 会把没修好链接的写入也计入」这个既有报告瑕疵一起做。做链接解析那一步会移动活库报告里的
788 这个数字，所以必须单独一批并有自己的证据。

### 活库影响：零，且可验证（复核后复验）

`python cli.py lint` 在修复前后数字逐项相同：`Scanned: 7923 files | Issues: 5314 | Auto-fixed: 0`、
`7. Broken Links: [FAIL: 788]`；活库计数不变；活库日期形页面仍为 **0**。

全量 pytest 由 777 增至 **780**（本批复核修复新增 3 例：标题/一级标题用核心名、不种下会被 auto-fix 变成
页面的链接、非法文件名被净化）。可失败性实测后恢复：把 `title` 改回 stem → 恰好第 1 例失败并报
`assert 'Concept_BrandNew-Thing' == 'BrandNew-Thing'`；把日期改回链接 → 恰好第 2 例失败。

## 删除 gram 索引的增量机制（无生产调用者的那条路）

上一批把读路径改成「不允许服务混合态」之后，增量那套就没有生产调用者了：`flush` 已不再把文档移出队列
（出队只能由重建完成），于是 overlay 永远为空，`compact` 与 `--compact` 成了空操作 —— 复核当时评为
「给下一个读者的陷阱」。本批把它整体切掉。

**删除**（`memory_gram_index.py` 231 行函数 + 8 行常量）：

- `flush_memory_gram_dirty()`、`compact_memory_gram_overlay()`、`_compact_gram_chunk()`、
  `prune_retired_gram_docs()`
- `DEFAULT_FLUSH_DOCS`、`COMPACT_GRAMS_PER_CALL`、`COMPACT_CHUNK_GRAMS`、`COMPACT_MAX_GRAMS`
- CLI `gram-index --compact`（连同分支）、MCP 工具 `compact_memory_gram_index`、`tools.py` 的导入与
  `__all__` 条目、README 里的 `--compact` 示例与说明

删除后基表只有一条变更路径：重建。退役标记也不再有一条「不重建就清掉」的捷径——它随重建一起清。

**故意保留**：`operational_memory_gram_overlay` 表及其索引与 `_SCHEMA_SENTINELS` 条目、
`overlay_row_count()`、`gram_index_usable()` / `rebuild_due_reason()` 里的 overlay 判据、读路径的
`_term_overlay()`。删表是活库上的 schema 变更，而活库目前**没有可用备份**，所以那一步单独走；判据留着
就是它本来的作用：一旦真有 overlay 行出现，索引立刻变成不可用且「到期」。

**表面变更要写明**：`gram-index --compact` 现在会**报错退出**（`unrecognized arguments`），不是静默忽略；
调用过 MCP `compact_memory_gram_index` 的客户端会看到工具消失。

### 测试

6 例随其被删对象一并删除，3 例改写：

- `test_gram_index_exactness.py`：两例原本用「materialise + merge」**搭出**毒化状态，现在直接断言存活的
  不变量（编辑过的文档不得因丢弃的词而被计入；索引搜索与全量扫描一致）。第三例原本 spy 两个被删函数来证明
  「读不排空」，改为**比较搜索前后的索引状态**——这是更强的断言（任何读路径写入都会被抓到）。
- `test_memory_gram_index.py`：`test_a_deleted_document_is_exact_before_any_prune` →
  `..._without_a_rebuild`（改为用重建清标记，即仅剩的那条路）；`test_compaction_preserves_results` 与两例
  flush 批次选择测试删除。
- `test_gram_maintenance_atomicity.py`：prune 那例随对象删除。
- `test_the_read_path_does_not_drain_a_backlog_beyond_the_cap` 保留，docstring 改成不再提「排空」。

### 证据

- 全量 pytest **766 passed**（772 − 6，删掉的 6 例全部是「测试被删对象」的）。
- 活库跑套件前后逐项相同：`operational_memory` 146,679、队列 0、**overlay 0**、
  `page_graph_edges` 29,837、`quick_check = ok`。
- 活库表面复验：`gram-index --if-due` 仍报「not due: 0 of 500」；`--compact` 报 `unrecognized arguments`；
  doctor 仍为 `usable=True ... due=False of 500`。
- 可失败性：把 `skip_doc_set()` 改成返回空集（模拟读路径不再压制陈旧基表）→ 改写后的那例失败并报
  `the dropped gram still credits the document: {1: 3}`；恢复即通过。
- 计数对账（复核质疑过 766/772 这个差）：`test_memory_gram_index.py` 17→14、`test_gram_index_exactness.py`
  8→6、`test_gram_maintenance_atomicity.py` 2→1，合计 **−6**，与被删对象数一致；另外 3 例为原地改写。

### 复核结论与随之修的内容

独立复核（只读子代理）判 **OK with notes**，无 P0/P1；删清单完整、保留面自洽、CLI/MCP/文档表面干净，
并独立确认「overlay 无人可写」这一前提（全树唯一的 `INSERT INTO ..._overlay` 在测试里）。

一条**判定为不成立**的项（C6「陷阱是否真的清了」），已按它给的清单修掉 6 处仍在暗示「增量路径还存在」
的文档：

- 模块 docstring 里 overlay 一节（原写「Recent writes … overlay entries winning」）与 dirty 一节（原写
  「dirty 文档在 overlay 里是权威的」）——这是最可能让下一个读者去找、或重新加回物化器的两句；现在写明
  **保留但无人写入**，以及精确性来自「拒绝服务」而不是「合并」。
- `accumulate_relevance` 的 `skip_docs` 说明（原写「权威值来自 overlay」）。
- `live_dirty_doc_count`（「awaiting materialisation」）、`dirty_breakdown`（「draining the queue」）。
- `db_store._create_memory_gram_tables`（「materialisation is a bounded maintenance step」，指向一个已不再
  描述任何维护步骤的 docstring 所在的位置）。
- `CONTEXT.md` 的一行摘要（原写 base + overlay + dirty set）。

另按复核意见把两处**断言弱于其名称**的测试 docstring 改成如实描述：`test_the_search_agrees_with_the_full_scan_after_a_dropped_gram`
实际只走到被拒绝后的投影扫描（两边的答案一致性仍值得钉，但它不是「合并后仍一致」），
`test_a_deleted_document_is_exact_without_a_rebuild` 的判据是计数，等值断言因投影联表会丢掉幽灵 rowid、
且结果窗宽于语料而无法因缺 skip 失败。

### 保留 overlay 表的判断（复核第 7 问）

复核明确建议**本批不删表**，理由与本批的取舍一致：保留的判据只可能「少服务」（退回精确扫描 + 一次重建，
且重建会清空 overlay，所以有终止解），却能挡住「旧版本进程或旧备份的带外写入者」；而删表不是纯删除，它
同时要改 `_SCHEMA_SENTINELS`（否则下一次写入会走完整 DDL 路径把表**重新建出来**）、要加一条
`_LEGACY_SCHEMA_PRUNES` 记录（否则活库永远留着它），这几件事与「零行为变更」的批次混在一起只会放大爆炸
半径。`GRAM_FORMAT_VERSION` 也不是干这个的：它管的是**基表内容可信度**，不是 schema；对的是 prune 台账。
删表批次该动的位置（DDL + 索引 + 哨兵 + prune 记录 + `overlay_row_count` 的 3 个调用点 + `_term_overlay`
与两处 overlay 循环 + doctor + 4 个测试 + 上述文档）已记录在案，待有备份后单独执行。

## 给 gram 索引定一个重建节奏

索引只在「上次重建以来没有任何文档被写入」时才精确（`gram_index_usable()`）；任何一次写入之后，搜索都会
退回精确扫描，直到有人重建。活库（146,679 文档）实测：

| | 索引路径 | 精确扫描 |
|---|---|---|
| 每次搜索中位数 | 0.295 s | 0.745 s |
| 8 条查询合计 | 2.906 s | 5.747 s |

一次重建约 **430 s**，且（上一批之后）整个重建期间**拒绝所有写入**，还会把 WAL 涨到暂存表（约 352 MB）
加基表重写（约 97 MB）的量级。按每次搜索省 0.355 s 算，一次重建要约 **1200 次搜索**才回本。

所以 `REBUILD_AFTER_WRITES = 500` 这个数不是按回本点定的，而是按「允许多陈旧」定的：真正稀缺的共享资源
是写入，任何省下的搜索时间都换不回一次因 20 s 锁预算失败而重试的摄取。常数注释里写了这套算术与理由。

### 触发位置

新增 `writes_since_rebuild()`（= 队列中**活文档**数）、`rebuild_due()`、`maybe_rebuild_memory_gram_index()`
作为唯一维护入口。读路径**未改**：`ensure_memory_gram_index` 在任何语料规模下都不重建陈旧基表，只重建
**缺失**的基表 —— 这正是本节奏赖以成立的规则，所以专门加了一条守卫测试盯着它（把读路径改成「due 即自愈」
会让该测试失败，已验证）。

两个触发点：

- 守护进程的定时维护块：在 `global_task_lock` 内、自动 lint 之后、**WAL checkpoint 之前**调用。顺序不是
  随意的：重建是本进程最大的一笔事务，也正是最需要随后那条 `wal_checkpoint(TRUNCATE)` 去回收的。
- `gram-index --if-due`（默认 dry-run 报告）/ `--if-due --apply`（重建）。不带 `--if-due` 时 `gram-index`
  行为不变（报告 + dry-run 预览）。**本机没有守护进程，所以定时那份触发不会发生**——`doctor` 里 `due=True`
  却没人执行时，索引会一直停在精确扫描上，这正是 `due=` 必须可见的原因。

阈值按**文档数**计，不按事务或时长：同一文档改十次只算一次，因为它只让后续搜索多付一次扫描差价。

### 可见性

doctor 的 `Memory Gram Index` 现在带 `due=False of 500`。这是**欠账的当前值**，不是故障：`usable=False`
时既有的降级告警已经会出现，`due=` 只是让操作者知道该不该跑 `--if-due --apply`。

**运营前提（必须写明）**：本机没有运行守护进程，所以定时那份触发不会发生，节奏实际由人按 `due=` 手工
执行 `--if-due --apply`。这不是缺陷，但它意味着「每 500 条写入重建」在无人值守的本机并不自动成立。

### 复核发现并修掉的 P1：阈值看不见「根本不能服务」的基表

独立复核（只读子代理）在「OK with notes」里指出一条 P1，**这条是真的，而且必须修**：`rebuild_due()` 原本
只看写入计数，因此对 `ready=False` 的基表（从未建立、被截断、或写入版本更旧）在 `live < 500` 时一律回答
「未到期」——而大语料的读路径拒绝建立缺失基表，于是**永远没人会去建立它**。这恰好是「抬版本号」或「恢复
备份」给大语料留下的状态：我上一批把 `GRAM_FORMAT_VERSION` 1 抬到 2 时，注释写的就是「这类库变成
`ready=False` 并重建一次」，但如果没人手工跑 `--apply`，`--if-due` 会说「未到期」。

修法：`rebuild_due_reason()` 成为唯一判据来源，返回**原因文本**而不是布尔值（这同时修掉了 dry-run 文案
里「0 document(s) written」无法解释「为什么建议重建」的自相矛盾），三条并列原因：基表不可用、有 overlay
行、写入计数达标。`rebuild_due()` 退化为 `reason is not None`。

活库实证（只读，未写入）：把 `GRAM_FORMAT_VERSION` 临时改成 3 模拟抬版本 →

```
Memory gram index is due: no usable base (never built, truncated, or an older format version);
0 retired marker(s) of 0 queued. Rebuild with `gram-index --if-due --apply`.
[OK] Memory Gram Index: usable=False ready=False ... due=True of 500
[WARN] ... memory_gram_index_unusable:live_backlog=0 ... due=True of 500 (rebuild: python cli.py gram-index --if-due --apply)
```

修复前同一状态打印的是「not due: 0 document(s) written」。复验后活库 state 仍为 `format_version=2`、
计数逐项未变，证明 dry-run 路径确实没有写入。

### 其余复核项（F2–F7，均已修）

- **F2**：重建期间状态文件里仍写着 "Running Scheduled Auto-Lint"，而唯一那行日志是**调用返回之后**才写的
  （调用本身是 `log.info` 的实参）。现在到期时先 `write_status(..., "Rebuilding memory gram index", ...)`。
- **F3**：那个调用点与「重建在 checkpoint 之前」的顺序**没有测试**，安全性只靠注释。新增 `tests/test_scheduled_lint.py`
  一例：走真实 `scheduled_lint_loop()`，用一个包装连接记录 `PRAGMA wal_checkpoint`，断言 `gram` 先于
  `checkpoint`。可失败性：把两块顺序互换 → `assert 3 < 1` 失败。
- **F4**：dry-run 文案 "Run with dry_run=False to rebuild." 是 Python API 用语，操作者的开关是 `--apply`；
  改为 ``Rebuild with `gram-index --if-due --apply`.``
- **F5**：`Memory Gram Index` 检查恒为 `ok=True`，其 WARN 只写 `live_backlog=N` 而不含阈值，并把
  `rebuild_memory_gram_index`（Python 符号）当成给操作者的指引。现在 WARN 带 `due=`/阈值并给出可直接运行的
  命令。
- **F6**：README 那段列了两个触发点却没写「定时那份需要守护进程在跑」，只有未发布的 CHANGELOG 写了这条前提
  ——对一个未来的读者来说，操作文档比变更日志更不诚实。README 已补一句，并指向 `doctor` 的 `Watchdog Status`。
- **F7**：`governance_store._gram_memory_candidates` 的 docstring 仍写着「积压太大所以读路径不排空」，而排空
  机制早已不存在（真实规则是「陈旧基表在任何规模下都拒绝」）。这句正好与本节奏的前提相反，已改正。属**既有
  文档漂移**，非本批引入，在此标注。

### 测试

`tests/test_gram_rebuild_cadence.py` **10 例**：阈值按文档计（改两次只算一次）、未达标时维护入口**一次都不
调用** `rebuild_memory_gram_index`（用 spy 断言零调用，而不是看返回文案）、达标时重建并把欠账清回 0、
**读路径仍拒绝重建陈旧基表**、维护入口**不继承** `AUTO_REBUILD_MAX_DOCS`（否则大语料永远无法重建，活库正
处在这个状态），以及三条「不可服务即到期」原因各一例。加上 `tests/test_scheduled_lint.py` 的新顺序例。

可失败性（均实测后恢复）：读路径改成「due 即自愈」→ 恰好那条守卫失败；`rebuild_due` 退回只数写入 → 恰好
三条「不可服务即到期」失败；把重建移到 checkpoint 之后 → 顺序断言失败。

全量 pytest **772 passed**（761 + 11）；活库计数、队列、`page_graph_edges` 与 state 行在跑套件前后逐项相同。

## 维护操作各自只取一次快照

`rebuild_memory_gram_index` 原先逐批提交暂存，然后在一个**后置**事务里清空队列。两件事之间开了一个
窗口：另一个连接在这段时间提交的写入，其标记会被这次清空一并抹掉，而基表里留下的是该文档**改写前**的
gram —— 结果是「无标记、无 overlay、基表陈旧」，而 `gram_index_usable()` 会报 `True`。活库上暂存阶段
约 430 s、每个文档各读一次，所以低 rowid 文档的窗口接近整个重建过程。

`prune_retired_gram_docs` 同属一类：决定「哪些文档已删」的快照读在事务之外。

### 修法

每个维护操作一次事务（`transaction()` 即 `BEGIN IMMEDIATE` + commit，且可重入）：

- 重建：投影读取、暂存、交换**全在一个事务里**。代价是期间写入者不再被交错，而是**失败关闭**
  （`DatabaseLockTimeout`，在 `BEGIN_LOCK_BUDGET_SECONDS` = 20 s 之后）——这本身是可见事件，不会被误
  当成一次干净的重建。暂存 DDL 仍在事务之前：本连接上半消费的读语句会挡住 DDL，这条注释保留。
- 清理：快照改为事务内的第一条语句。事务外读时，期间被删的文档 —— 或复用了已释放 rowid 的新文档，
  触发器里那个 `NOT EXISTS` 守卫不会再给它打标记 —— 可能在标记被清掉的同时被剥掉基表倒排，等于把一个
  活文档从基表里移走且不再跳过它。全表扫描足够长，这个窗口是真实的。

**运营含义**（必须写明）：重建期间写入者会在 20 s 预算耗尽后被拒。这是一次维护操作应有的排他性，
但它改变了「重建是一次可以随时跑的后台动作」这个印象；跑之前应确认没有写入正在进行。

还有一条**磁盘**含义：整个重建在一个事务里，所有脏页在提交前都留在 WAL 中且无法被 checkpoint 越过该
事务的起点，因此 WAL 会涨到「被触达页集合」的量级 —— 暂存表一项在活库约 352 MB，加上基表重写（约
97 MB 倒排）与删除/插入的翻动。磁盘不足时会在重建中途报 `SQLITE_FULL` 并回滚：失败关闭、不损坏，
但这次重建白跑。同时定时的 `wal_checkpoint(TRUNCATE)` 在该事务进行期间无法完成。

重建自动触发只在读路径且**基表缺失**时发生（≤ `AUTO_REBUILD_MAX_DOCS`），此时约 6 s，仍在写入者的
20 s 等待预算内，不会把写入者卡死；但这条上限现在同时决定了「读会拒写入多久」，该约束已写进常量注释。

### 测试

新增 `tests/test_gram_maintenance_atomicity.py` 2 例：在维护操作到达快照点后（用一个钩子把执行停在那
一刻），从第二个连接尝试 `BEGIN IMMEDIATE` 并断言**被拒**。

- 重建那例是**真实回归测试**：修复前报 `DID NOT RAISE`（旧代码在调用 `extract_grams` 时并不持锁，
  正是那个窗口）。
- 清理那例**不能区分修复前后**（它的扫描本来就在事务内，事务之外的是快照，而快照读取与事务之间没有
  可挂观察点的调用），其 docstring 已明说这一点。保留它是因为「扫描期间写入被拒」正是本修复依赖的
  性质，日后若有重构把扫描挪回事务之外，它会失败。
- `_compact_gram_chunk` 的 gram 清单仍在事务外读，判定为无害（外部读只决定合并哪些 gram，合并与删除
  都在锁内完成），未改。

复核（只读子代理）结论为 **OK with notes**：无 P0/P1，并**推翻了我自己提出的一条担忧** —— 我担心
「旧代码可以在批次之间中断后续跑」这一能力被本次修改取消，复核用证据纠回：暂存表在**每次重建开头**都会
被 `DROP ... IF EXISTS` 后重建（全仓 grep 只有那几处引用），所以那份「能力」本来就不存在；本次变化只是
崩溃时连暂存行一起回滚，而不是留下一个孤儿表。另外复核确认了三件事：事务内提前 `return` 走的是 commit
而非 rollback；`_compact_gram_chunk` 的事务外读确实无害；嵌套调用会并入调用方事务，因此函数返回的成功
文案不代表已提交（无生产调用方嵌套，已写入 docstring）。

全量 pytest **761 passed**。

## lint 用一份列表回答了两个不同的问题，两个方向都出错

`lint_vector_lake` 先过滤出一个文件列表，然后拿它同时决定「哪些文件要 lint」和「哪些链接目标存在」。
这两半朝相反方向各错了一次。

### 方向一：生成物被当成节点 lint，`auto_fix` 还会重命名它

过滤器里只有 `index.md` / `log.md` / `overview.md` 三个名字，于是
`orphan_pages.md`、`wiki_link_stats.md`、`Synthesis_log.md` 作为节点被检查 —— 它们不以合法前缀开头、
也没有 frontmatter，于是被判「Does not start with valid prefix」，且在 `auto_fix` 下**被改名为
`Concept_orphan_pages.md`**。

测试钉住了这条路径：修复前日志给出

```
Auto-fix rename failed for orphan_pages.md: Error during atomic rename:
Schema Violation: Missing required frontmatter field 'id'.
```

也就是说重命名**是被真正发起过的**，只是恰好在 schema 校验上被拦住（报告没有 `id`），不是被意图挡住 ——
真实 wiki 里的生成物若带 frontmatter，这一步就会成功，并把它从读它的东西眼前藏掉。

### 方向二：指向 `index`/`log`/`overview` 的链接永远解不开

这三个恰好是被过滤掉的名字，于是它们不在 `all_keys` / `link_target_map` 里：指向它们的链接被判破链，
`auto_fix` 会在真页面旁边再造一个存根。

### 修法

**lint 范围** = 磁盘上所有 `.md` 减去 `node_vocabulary.NON_NODE_WIKI_FILES`（六项，即批 8 归一的那套
集合）；**链接目标** = 磁盘上所有 `.md`，lint 与否不影响「页面是否存在」。两者分开后，批 8 那条「六项对
三项」的差异在这里闭环：`tool_lint` 不再自己维护第三套清单。

新增 `tests/test_lint_scope_and_link_targets.py` 4 例：生成物不被当节点扫、指向生成物的链接能解开、
`auto_fix` 既不重命名也不造存根、以及指向确实不存在的页面的链接**仍然**被判破链（防止前三条是靠关掉破链
检测换来的）。**修复前 3 例失败，1 例通过**（第 4 例是覆盖面守卫，两版都通过是预期的）。

### 活库：可观测变化为零

两套排除集合的差异只落在 `orphan_pages.md`/`wiki_link_stats.md`/`Synthesis_log.md` 三个文件上，而它们
在活库**都不存在**，因此 `files` 集合在活库上与改动前逐一相同、链接解析集合并未新增任何条目 —— 除了
代码路径本身，活库 lint 输出不变（复验：`Scanned: 7923 files`、`2. Naming Compliance: [PASS]`、
`7. Broken Links: [FAIL: 788]`）。同期 `11. Semantic Garbage Collection` 由 2 变 5 属**时间衰减**检查，
与本次改动无关（文件集合未变）。

全量 pytest **759 passed**。

## 索引只在答案精确时才允许作答

`gram_index_usable()` 原先回答的是「基表够不够新」，而调用方读成「答案对不对」。这两件事在
「基表 + overlay 相加」的状态下并不等价，差值有实测。

### 缺陷（P1）与实测

一个活文档被编辑、丢掉某个词，然后按下述运维顺序处理：

1. `flush_memory_gram_dirty()` 把它的新 gram 写进 `operational_memory_gram_overlay`，并**把它从
   队列里删掉**；
2. `compact_memory_gram_overlay()` 把 overlay 合进基表。

第 2 步修不好第 1 步：`_compact_gram_chunk` **只遍历 overlay 里存在的 gram**，而被丢掉的词按定义
不在其中，所以基表里 `(被丢掉的词, 该文档)` 这一行永远不会被重访。而此时队列项已经没了 —— 队列项
恰恰是读路径跳过该文档陈旧基表行的唯一依据。于是文档为它已不再包含的词得分。

实测（`accumulate_relevance` 层）：应为 `{}`，实得 `{doc: 30}`。新增测试
`test_a_document_is_not_credited_for_a_gram_it_dropped` 复现该状态，**修复前 7 例新测试中 3 例失败**
（含 `test_materialising_a_document_does_not_retire_its_queue_entry`、
`test_compaction_does_not_make_the_base_authoritative`）。

用户可见层有两个面，**只有一个被掩蔽**：2 字以上的词会经 `_accumulate_composite` 回读
`operational_memory_index` 做精确校验，把陈旧候选滤掉，所以「为丢掉的词计分」这条路不必然表现为检索
结果错。另一个面是直接可见的：旧 `ensure` 在队列非空时先 flush、flush 又把文档踢出队列，于是**刚编辑
完、尚未被 flush 的文档基表行被跳过、overlay 里又还没有它** —— 按它新内容去搜会返回 `[]`，而不是返回
这个文档。本批的判据同时关掉这两面。

### 定下的不变量

**基表是权威的，当且仅当：队列里没有活文档，且 overlay 为空。**

- 活文档在队列里 ⇒ 它的基表行陈旧，跳过它们才是对的；留在队列里是唯一让读路径继续跳过的手段，所以
  `flush` 不再让文档出队（出队只能由重建完成）。
- overlay 非空 ⇒ 同一文档的倒排分裂在两个结构里，而读路径对两者是**相加**，overlay 的语义却是
  **替换** —— 两者必须择一，这里选择不允许服务这种状态。
- **已删标记（retired）不在此列**：读路径对队列内文档一律跳过基表行，所以它们不可能贡献，不影响精确性。
  因此可用性判据读的是 `dirty_breakdown()` 的三元组而不是总数。

### 因此不再有增量修复这条路

没有任何增量步骤能消掉「被丢掉的 gram」的基表行，于是拖队列、合 overlay 都不构成通往可用索引的路 ——
只有 `rebuild_memory_gram_index()` 是。据此：

- `ensure_memory_gram_index()` 不再调用 `flush_memory_gram_dirty()` / `compact_memory_gram_overlay()`；
  继续走精确扫描，恢复方式是显式重建 —— 这正是 MCP `memory_gram_index_status` 早已写明的路径。
- `GRAM_FORMAT_VERSION` **1 → 2**：上一版代码留下的库可能正好满足新判据（无活标记、无 overlay）却压着
  一个陈旧基表 —— 两种状态在计数上无法区分，只能靠版本号。抬版本把这类库变成可见的 `ready=False`，
  小语料自动重建、大语料留待显式重建。
- `_compact_gram_chunk()` 把本次合并到的文档**重新标记回队列**：合并一个 gram 并不会让基表对这些文档
  变权威，反而因为它们仍在队列里而基表被跳过，需要重新标记才不会出现「看起来回到了精确状态、实际仍在
  为丢掉的词计分」。

### 独立复核（1 个 P1 由复核找出，1 个 P1 与本批引入的设计问题一并修正）

复核（只读子代理）确认了机制、C2/C3/C6 三条断言成立，并找出两处必须先处理的问题与本批自己引入的
一个设计缺陷：

- **`tool_doctor.py` 自己复述了一遍旧判据。** 它算的是 `gram_ready and live_backlog <= cap`，正是「读
  路径会排空所以小积压没关系」那句话的镜像。读路径不再排空后，它会把一个 146k 文档库上只有 1 条积压
  的索引报成 `usable=True`，而实际每次检索都在全表扫描 —— 诊断面在撒谎。改为直接取
  `memory_gram_index.gram_index_usable()`，并把 overlay 行数一并列出；旧注释里那段「小积压会自愈」
  一并删除，并说明为什么不再在这里复述判据。
- **上一版留下的库会被误判为可用。** 旧 `flush` + 旧 `compact` 恰好制造出「无活标记、无 overlay、基表
  陈旧」这种与干净状态在计数上无法区分的状态。已由版本号抬升关闭（见上）。可失败性已证：把常量改回 1
  后 `test_a_base_from_the_previous_release_is_not_trusted` 报 `assert True is False`。
- **本批第一版让「读路径就地重建小语料」每次写后又重建一次。** 在 2000 文档上限处，重建约 6 s 而全表
  扫描约 32 ms（按代码自述的 3 ms/文档与 0.27 µs/次比较推算，属估算非实测），差约 190 倍，且没有冷却
  窗口。已收窄为**只在基表缺失时**（从未建过 / 表被截断 / 版本不符）才在读路径中构建：陈旧基表一律交回
  精确扫描，不管语料多小。相应地 `AUTO_REBUILD_MAX_DOCS` 的含义从「可自愈的积压上限」改为「允许读路径
  自动重建的语料上限」。
- **本批的「读路径不排空」测试原本不可能因正确理由失败**：`ensure` 会吞掉所有异常并降级到精确扫描，
  而精确扫描正是该测试的比较基准，所以让 stub 抛异常等于让测试恒过。已改为**记录调用并断言零调用**。

### 未做，以及为什么

**没有删除 `flush`/`compact` 与 overlay 表。** 它们现在是 overlay 的有界维护，不再是「让读更早可用」的
手段 —— 这套增量机制整体是「为一条不可能精确的路径维护的剩余物」，且 `flush` 已无生产调用者。删除它要
连带处理 `gram-index --compact`、MCP `compact_memory_gram_index` 与 `tools.py` 的导出，属独立批次，
不在本批夹带。批 4 为 `flush` 修的队列头阻塞仍然有效，只是适用面收窄成维护路径。

复核提出、本次**未修**的三条，按既有缺陷登记：

- **重建与清理对并发写者不是原子的（既有）**：`rebuild_memory_gram_index` 逐批提交暂存后，在一个**后置**
  事务里清空整张队列，另一个连接在此之间提交的写入会连标记一起丢失；`prune_retired_gram_docs` 的 retired
  快照也取在事务之外，叠加 rowid 复用窗口可能误删活文档。本批的收窄已把读路径触发重建的场景压回「基表
  缺失」，因此曝光面回到改动前水平；修法（按暂存文档集删标记、快照移入事务）留给独立批次。
- **`_record_state` 被传入两次 `base_docs`**（`_compact_gram_chunk` 与 `prune_retired_gram_docs` 的调用点），
  使 `postings_count` 被写成 `base_docs`。该字段无任何读取方，惰性缺陷，登记不修。
- **基表 postings 以 `operational_memory_index.rowid` 为键**，而 SQLite 不承诺 rowid 跨 `VACUUM` 稳定；
  活库此前有两次 VACUUM 记录。潜在、本批无法验证，登记。

### 活库：一次未授权的重建（已消除痕迹）

本批定案前的一次只读探针写错了环境变量（`VECTOR_LAKE_MEMORY_ROOT` 不是本项目识别的变量，正确的是
`VECTOR_LAKE_MEMORY_DIR`），于是脚本跑在了**活库**上：插入了一条测试文档 `mem_x`，并触发了
`rebuild_memory_gram_index()`。处置与结果：

- 已按精确目标删除 `mem_x`（1 行），并 `prune_retired_gram_docs()` 清掉它在基表里的 57 个 gram；
- 复验：`operational_memory` = 146,679、`operational_memory_index` = 146,679、`quick_check = ok`、
  `change_sets`/`mutation_outbox`/`governance_queue` 计数与事发前逐项相同；
- **不可逆的部分**：gram 基表由 414,914 gram 变为 420,913 gram（按当前投影重建），队列 111,564 → 0，
  overlay 153 → 0。事件前的队列构成已不存在；但重建后正确的队列本应为空，故这是取证信息的损失，
  不是正确性损失。此后活库的该索引首次处于「基表 = 当前投影」的干净状态。

## 「这个文件到底是不是节点」的答案，原先写在六个地方

`{index.md, log.md, overview.md, orphan_pages.md, wiki_link_stats.md, Synthesis_log.md}`
—— 这套「不是知识节点的 wiki 文件」清单原先散落在 **6 处**，且形态各异：内联字面量、裸元组、
模块常量。六处取值当时一致，所以没有任何可见故障；重复真正买到的东西是：下一次修改会落在当时凑巧
被打开的那一份里。

其中两处连「承载信息」都算不上：`runtime_health` 与 `tool_doctor` 各自把本地副本传给
`wiki_page_keys`，而该函数的默认参数**本来就正好是这套集合** —— 它们做的事是用一份副本替换默认值。

现在归一到零 import 叶片模块 `node_vocabulary.NON_NODE_WIKI_FILES`（`frozenset`），
`wiki_utils.SYSTEM_WHITELIST` 变成**同一个对象**而非同值副本（批5 已用过这个模式）。取值与判定
行为逐项复验不变：`identity: True`、集合相等、`validate_schema` 对六个文件名照旧跳过。

### 与另一套「三文件清单」的区别，以及为什么没有一并合并

另外还有 **7 处**持有更窄的 `{index.md, log.md, overview.md}`（`governance_store` ×2、
`tool_delete`、`tool_ingest`、`tool_lint`、`tool_rename`、`watchdog_app`），`indexer:1138` 甚至只有
两元素（缺 `overview.md`）。**这不是同一套集合**：它回答的是「哪些文件可以直接读/删」，与「是不是
节点」是两个问题，因此本批**没有**动它们，只在所有者注释里点名区分。

`tool_lint.skip_files` 也保持三元素不动，理由与直觉相反：它在 `:81` 过滤的是**要 lint 哪些文件**，
若改用六元素集合，这些文件会被移出待检清单、其 key 不再进入 `all_keys`，于是指向它们的链接**反而
会被判成破链**、`--auto-fix` 还会为它们造出存根。lint 需要同时具备两套集合（一套决定检查范围、一套
决定链接解析），今天它把两者压成一个列表 —— 这是独立缺陷，记录在案，未在本批改动。

### 新增 `tests/test_non_node_wiki_files.py`（11 例）

守卫用 AST 统计**字符串常量**里出现的成员数，而不是扫文本，因此单引号藏不住副本、
「元组 + 字面量」也不会被重复计数（这一点强于批5 的文本扫描守卫）。阈值设为 **4**：三元素清单
（7 处）合法通过，四成员及以上的重声明触发失败。

阈值自证：`test_the_guard_fires_on_a_redeclared_set` 用合成源码证明守卫能失败，
`test_the_guard_tolerates_the_narrower_three_file_list` 证明三元素清单不被误伤；另外做了真实树验证
—— 把 `tool_doctor` 的那份副本手工还原后，守卫报出
`{'tool_doctor.py': [...六个成员...]}` 并给出修复指引。边界（看不见变量拼装的集合，也看不见
`vector_lake/*.py` 之外的成员清单）写在模块 docstring 里。

全量 pytest **747 passed**。

## 审计的「5 个 import 环」经实测不成立：0 个加载期环

审计把 `wiki_utils`（fan-in 30）周围的 5 条路径记为「层反转/环」，并据此建议重排分层。用 AST
区分**模块级**与**函数级**导入后重算，结论相反：**模块加载期没有任何环**。

那 5 条都是把两类边合并后跑 SCC 得到的结果 —— 属于**概念上的层反转**，不是导入缺陷。包之所以一直
可加载，正是因为所有「向上」的边都延迟到了函数作用域。真正的双向对只有 **2 对**：

| 出边 | 延迟导入位置 | 反向为何成立 |
|---|---|---|
| `wiki_utils` → `mutation_coordinator` | `write_markdown_file` | coordinator 在模块级导入 `wiki_utils` |
| `db_store` → `tool_timeline` | `delete_node_cascade` | `tool_timeline` 在模块级导入 `db_store` |

量化验证：把 `wiki_utils` 那条延迟导入提到模块级，包立刻不可导入 —— 形成 5 模块 SCC
（`db_store → defense_hook → mutation_coordinator → purpose_contract → wiki_utils`），
解释器报 `cannot import name ... from partially initialized module`。**也就是说这条延迟导入是承重的，
不是可以顺手「清理」的坏味道。**

两对都不是意外，且 `db_store` 那一对还额外承重：`tests/test_timeline_projection.py` 依赖调用方在调用
时刻解析 `tool_timeline` 的绑定，才能打进 `sync_timeline_events_for_claim_delta` 模拟投影失败、断言
规范事务回滚。把函数挪到下层会**静默取消这个故障注入点**。

### 新增 `tests/test_import_layering.py`（3 例），并如实标注它的价值边界

它会自动发现这类问题，但**不是因为**它拦住了环：环一旦出现，`tests/conftest.py` 导入包即失败，整个套件
在**收集阶段**就报错，根本轮不到这个测试。它的真实价值更窄：

- 只读文件、不导入包，所以能在**包完全无法导入**的状态下运行（用 `importlib` 在故意破坏的树上实测过）；
- 它指出形成环的**具体模块集合**，而不是丢下一串 partially-initialized-module 的链条。

第一点和第三点分别对应 `test_there_is_no_module_level_import_cycle` 与
`test_only_the_two_documented_pairs_are_deferred_and_upward`；第二点是
`test_the_cycle_checker_detects_a_synthetic_cycle`，用于保证检查器自身可失败。边界（只扫
`vector_lake/*.py`；看不见 `importlib` 动态导入）写在模块 docstring 里。

### 未做，以及为什么

**不重排分层。** 30 个导入者的底座模块、零个可测量的缺陷、而唯一的替代方案（把写入闸门的所有权
上移）会改变 `write_markdown_file` 的契约 —— 这是独立批次的规模，不是本轮的范围。两处延迟导入已就地
加注释说明它们是承重的、以及原因，`wiki_utils` 那条的真实收敛方向（低层只提供 `atomic_write_text`
原语，写入闸门由上层包裹）记录在案。

全量 pytest **736 passed**。

## `lint --auto-fix` 的存根类型错误，使这些“修复”从未真正落盘

`lint_vector_lake(auto_fix=True)`（可经 `cli.py lint --auto-fix` 与 MCP 工具触达）为破链创建存根时，
文件名按目标自带的前缀命名，而 frontmatter 里的 `type` **硬编码为 `concept`**：

```python
stub_filename = f"Concept_{target}.md" if not target.startswith(valid_prefixes) else f"{target}.md"
...
"type": "concept",
```

而写入走 `execute_mutation_plan` → `validate_schema`，后者会校验文件名前缀与 `type` 是否一致。实测：
链接 `[[Vendor_Missing-Thing]]` 产生 `Vendor_Missing-Thing.md` + `type: concept`，被拒：

```
Schema Violation: Filename prefix 'Vendor' does not match frontmatter type 'concept'.
```

异常被 `except Exception` 捕获后只写一行 `log.warning`（`tool_lint.py:55`，`fixes_applied` 不增加）。
所以真实后果**不是“写出了一个非法页面”**，而是：**页面从未被创建、破链仍然破着、而 lint 报告里对此
一字未提** —— 看起来像是“修过了、只是没什么可修”。

修复：文件名前缀与 `type` 是同一个决定，两者都从上一批建立的词表所有者推导（`node_vocabulary`），
并让正文的 H3 插槽跟随类型（`### 物理机制` 是 concept 的插槽，用在 Vendor 页上同样是错的）。同时
与 `tool_query` 的存根创建保持一致：拒绝生成物类型（`System_*`），因为 `indexer` 会跳过这类页面，
写出来只会让这条检查通过而图里永远没有该节点，把缺口藏起来。

### 实测边界（一并钉住，不留含糊）

| 目标 | 类型 | 插槽 | `validate_schema` |
|---|---|---|---|
| `Vendor_Missing-Thing` | `vendor` | `### 组织架构与商业模式` | 通过 |
| `Standard_Missing-Thing` | `standard` | 该类型的首个插槽 | 通过 |
| `Source_Missing-Thing` | `source` | 无声明插槽 → 回退到通用行 | 通过 |
| `Bare-Thing` | `concept` | `### 物理机制` | 通过 |
| `System_Missing-Thing` | — | — | 按设计**不写**（生成物命名空间）|
| `Synthesis_Missing-Thing` | — | — | **仍不写**（见下）|

`Synthesis_*` 依旧不被生成，但原因与本批无关：`schema_validator` 要求 Synthesis 页必须带
`## 核心合成论点 (Core Synthesized Claims)` 与 `## 支撑拓扑 (Supporting Topology)`，而通用存根正文
不产出这两节。修复前它先被类型不匹配挡住，修复后改由这条规则挡住 —— 两种情况下都没有页面。
该限制已写成测试固定下来（避免将来有人让它“悄悄开始”产出非法 Synthesis 页），并在代码注释里注明。

`tests/test_lint_stub_type.py` 新增 6 例；**修复前其中 2 例失败**（断言 `stub.exists()` 为真而实际为假），
修复后全绿。全量 pytest **733 passed**。

### 仍未处理（记录在案）

`tool_lint` 与 `tool_query` 各自维护着一个存根创建器：前者直接写文件、吃 `auto_fix` 开关；后者走
`execute_mutation_plan` 并产出 `strategic_scope` 等字段。本批只修了前者的类型与插槽错误，**没有**把
两者合并 —— 合并会改变 lint 的写入路径与 `fixes_applied` 语义，属于独立批次。

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
