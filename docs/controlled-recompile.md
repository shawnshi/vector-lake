# 专用受控重编入口（限定60份）

`recompile-ingest` 是本地操作者入口，**不是**普通 `sync` 的新扫描策略，也不是认证或 OS 隔离边界。它只接受冻结78项清单中的39份证据缺口与21份隔离资料；18份有效拒收不得纳入。两个输入文件均须提供 SHA-256，源文件的 SHA-256、MD5、大小与 mtime 必须仍匹配清单。

```bash
python cli.py recompile-ingest --plan <frozen-plan.json> --plan-sha256 <digest> --approval <read-approval.json> --approval-sha256 <digest>
# 上述只验证，不登记或派发；实际交接再显式增加 --apply --batch-size 1。
```

读取批准文件的结构为 `{"version":1,"request_id":"<unique-id>","plan_sha256":"<digest>","read_scope":"public_only","entries":[...]}`。`entries` 必须恰好覆盖60份；每项含 `filepath`、当前 `sha256`、`classification:"public"` 和 `classification_evidence`（12–1000字符）。**不能由目录、domain、URL或程序默认值替操作者填充公开性证明**；未知/私人资料不被这个入口接收，SHA-256也只绑定批准制品，不证明其分类主张。当前入口不提供私人资料的隔离授权模式。

交接复用原生任务包、单实例Runner、claim/lease及 `finalize_ingest`；使用独立 `ingest_recompile` 作业类型，旧Runner不会把它领取后按duplicate关闭。只有持久请求、白名单、当前字节与租约全部匹配，才允许本次任务不走C1重复关闭；普通作业的去重不变。Canonical Source命名冲突、其他活跃/待处理任务、新出现的当前版本完成回执、旧账本漂移或不明/共享Source归属均会阻断交接。模型只读取任务目录中的已校验只读源快照，不再打开可变化的原raw；只读属性/工作目录不构成OS安全沙箱。快照在真实finalization后清理，未完成任务的快照保留供恢复。模型不获得混合Wiki候选或私人purpose正文，只能按当前源独立编译或给出真实来源拒收；非Source页保持原生create-only规则。

旧jobs/result_json保留；仅真实finalization才更新processed_files。请求登记的`completed`只是授权记录，**不是模型完成或出版回执**；输出`DISPATCHED_NOT_COMPLETE`也不是完成。新回执另记录request、批准制品及当前SHA-256绑定。失败作业只能通过同一显式入口重交接，保留失败次数与等待时间；更换request_id不会重置同内容的确定性失败预算。先交接1份并验证真实出版/拒收回执，才继续余下对象。新代码须按 [README 的后端暂停与恢复规范](../README.md#后端暂停续批耗时与探针缓存)加载到常驻Runner；磁盘修改或派发成功不证明新服务已激活。

经操作者明确决定，可用 `--defer-filepath <approved-raw> --defer-sha256 <digest>` 延后一份归属不明的对象，再沿原入口交接其他对象。这不覆盖、改绑或读取该旧Source，不移除60份冻结白名单中的成员，也不改写拒收与处理账本。延后要求当前指纹匹配、没有未完成原生作业，且确实命中归属未证明的门；同一请求仅允许一份，持久记录单列为 `ingest_recompile_deferral`。其 `completed` 只表示延后登记，**不是来源重评完成**；登记后按固定幂等标识校验完整绑定，撤销或损坏必须阻断，旧参数不得复活或换对象，本入口不提供解除延后。交接结果另返回 `deferred` 数量，该对象不能进入模型或finalizer。
