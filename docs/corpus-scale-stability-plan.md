# Vector Lake 12k+ 稳定性与性能验证方案

基线：隔离分支 `codex/gbrain-borrowing`，commit `fc5ee4bd53e0936add5023e98a623921e606ad2c`，在既有未提交优化之上继续测试。活动 RC 仅作只读对照，不部署或写入真实知识库。

## 验收范围

- 至少 12,000 个 synthetic nodes 与 11,999 条边。
- 实际 SQLite FTS5、exact identity、graph expansion、rerank、result budget、index decoder/cache 与每线程 SQLite connection。
- 单线程热检索、8 线程并发、冷 exact identity、冷/热 index load 和进程 RSS 增量。
- 强制关闭远程 query embedding，fixture 位于临时目录并在运行后删除。

默认 SLO：

| 指标 | 门槛 |
|---|---:|
| corpus | `>= 10,000` nodes |
| cold index load | `<= 2,500 ms` |
| warm index load p95 | `<= 25 ms` |
| serial search p95 / max | `<= 250 / 1,000 ms` |
| 8-worker concurrent search p95 | `<= 750 ms` |
| FTS failure fallback p95 | `<= 750 ms` |
| concurrent throughput | `>= 20 qps` |
| error rate | `0` |
| search RSS delta | `<= 512 MiB` |
| cold identity index build | `1` pass per generation |

## 问题登记

- `PERF-01`：现有测试只有局部 hot-path/内存断言，没有可重复的 10k+ 端到端响应 SLO。修复目标是加入 `vector-lake-corpus-scale-benchmark/v1`。
- `PERF-02`：验证 generation-scoped exact identity cache 在并发冷启动时是否发生重复 O(N) 构建；若 build pass 大于 1，必须实现 single-flight。
- `STAB-01`：验证多线程 FTS connection、bounded result、FTS 故障 fallback 和无远程 embedding 条件下是否出现异常或 recall miss。
- `STAB-02`：持续 FTS 故障不能按请求写 ERROR 形成日志风暴；首条记录后按 backend 限频，并暴露 suppressed 计数。
- `PERF-03`：验证 index decoder 的冷加载成本与热缓存身份复用；不以 monkeypatched search loader 代替该项证据。

运行命令：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
python benchmarks/corpus_scale_benchmark.py `
  --workspace C:\Users\shich\MEMORY\scratch\vector-lake-stability-20260822 `
  --nodes 12000 --serial-queries 120 --concurrent-queries 320 `
  --workers 8 --fail-on-slo
```

benchmark 只证明指定硬件、fixture 和代码 revision 上的响应表现。活动 MCP transport、真实 6,979-node 数据、语义 readiness 与生产 golden retrieval quality 必须分开报告。
