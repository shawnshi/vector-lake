# Vector Lake 12k–25k 稳定性与性能报告

测试日期：2026-08-22。代码基线为 `fc5ee4bd53e0936add5023e98a623921e606ad2c` 上的隔离分支 `codex/gbrain-borrowing`。远程 query embedding 全程关闭，synthetic fixture 在临时目录生成并自动删除，没有写入真实 MEMORY canonical 数据。

## 结论

12,000 与 25,000-node 门禁均通过。正常检索、8-worker 并发、冷/热 index load、exact identity、FTS 故障 fallback、结果预算和 RSS 均在既定 SLO 内，错误率为 0。

发现并修复两项问题：

- `PERF-02`：8 个并发冷请求各自执行一次 O(N) identity index 构建。修复为 generation cache single-flight。
- `STAB-02`：持续 FTS 故障按请求写 ERROR。修复为按 backend 30 秒限频并暴露 suppressed 计数，检索降级回执不变。

## 修复前后

| 12k 指标 | 修复前 | 修复后 |
|---|---:|---:|
| cold identity build passes / 8 requests | 8 | 1 |
| cold identity p95 | 535.10 ms | 73.77 ms |
| serial search p95 | 1.11 ms | 1.19 ms |
| concurrent search p95 | 9.34 ms | 9.07 ms |
| concurrent throughput | 1,473 qps | 1,686 qps |
| search errors | 0 | 0 |
| FTS outage ERROR / 20 requests | 20 | 1（19 suppressed） |
| FTS fallback p95 | 未纳入基线 | 25.29 ms |
| search RSS delta | 采集器失效 | 62.05 MiB |

identity cold-concurrency p95 下降约 86.2%，且重复 O(N) 分配由 8 份降为 1 份。常规热路径变化属于运行噪声范围，没有因互斥修复退化。

## 最终门禁

| 指标 | 12,000 nodes | 25,000 nodes | SLO |
|---|---:|---:|---:|
| cold index load | 412.37 ms | 904.76 ms | <= 2,500 ms |
| warm index load p95 | 0.47 ms | 0.54 ms | <= 25 ms |
| serial p95 | 1.19 ms | 1.24 ms | <= 250 ms |
| serial max | 215.16 ms | 326.00 ms | <= 1,000 ms |
| 8-worker concurrent p95 | 9.07 ms | 8.16 ms | <= 750 ms |
| throughput | 1,686 qps | 1,707 qps | >= 20 qps |
| FTS fallback p95 | 25.29 ms | 51.51 ms | <= 750 ms |
| RSS delta | 62.05 MiB | 82.21 MiB | <= 512 MiB |
| error rate | 0 | 0 | 0 |
| identity build passes | 1 | 1 | 1 |

25k 数据来自包含 single-flight、RSS 修复、fallback SLO 与日志限频的最新工作树；20 次 FTS 故障只记录 1 条 ERROR，telemetry 显示 `fts5: 19` suppressed。

## 活动运行时只读对照

- Doctor：Wiki/JSON/SQLite 均为 6,979，projection committed，Watchdog idle，Write Gate clean，GEMINI_API_KEY 可用。
- 活动 RC 暴露 51 MCP tools，source revision `stale=false`；它不是本隔离分支的新代码。
- 5 次 `Vector Lake`、`top_k=1` 搜索均 `embedding_bypassed_by_fts=1`，内部冷峰值 457.78 ms、最后一次 12.35 ms。
- MCP fast lane 的 execution max 526.14 ms、queue wait max 2.08 ms；外层首调用墙钟 14.2 s、后续 63–81 ms。因此首调用额外约 13.7 s 位于 Vector Lake executor 之外，不能归因于 corpus search，也尚未由本仓库修复。

## 证据边界

- synthetic corpus 证明结构规模和运行时机制，不证明真实语义检索质量；真实 golden dataset 仍应使用 retrieval benchmark 验证。
- 活动数据只有 6,979 pages，尚未实际扩容到 10k；本轮没有制造或导入真实资料来凑数量。
- Semantic Readiness 仍为 `not_ready`，包括 unsupported/provisional/coverage 治理债务；这与响应性能分开报告。
- 未部署、未 commit、未 push，也未触发真实 sync、embedding、GC 或治理写入。
