# Vector Lake retrieval benchmark

`python cli.py retrieval-benchmark <dataset.json>` 运行只读检索评估。默认关闭远程 query embedding；只有显式提供 `--allow-remote-embeddings` 才沿用配置的 embedding provider。

运行环境必须已有通过 sidecar、digest 和 generation 校验的 committed retrieval projection；索引缺失、未提交或返回非 `EvidenceResults` 时评估失败关闭。`retrieval-v1.template.json` 只是占位模板，必须先替换 query 与 canonical key，不能作为质量基线直接执行。

数据集使用 `vector-lake-retrieval-benchmark/v1`：

```json
{
  "contract_version": "vector-lake-retrieval-benchmark/v1",
  "dataset_id": "medical-it-golden",
  "dataset_version": "2026-08-22",
  "top_k": 5,
  "thresholds": {
    "recall_at_k": 0.9,
    "mrr": 0.75
  },
  "queries": [
    {
      "id": "hospital-emr-grade",
      "query": "某医院电子病历评级",
      "relevant_keys": ["Institution_某医院", "Source_某医院评级公告"],
      "domain": "Medical_IT",
      "include_history": false
    }
  ]
}
```

报告包含数据集 SHA-256、evaluator version、P@K、R@K、MRR、nDCG@K、逐查询排名和阈值失败项。数据集内容、查询和预期 key 属于评估资产，应与报告一起版本化；运行器本身不会把结果写入 canonical 数据库。

## 12k+ corpus 稳定性与延迟

`corpus_scale_benchmark.py` 在临时目录构建默认 12,000-node synthetic index 和 SQLite FTS5 corpus，测量冷/热 index load、单线程与 8-worker 检索、cold identity single-flight、错误率、吞吐和 RSS 增量。它固定关闭远程 query embedding，结束后删除 fixture，不读写真实 Vector Lake canonical 数据。

```powershell
python benchmarks/corpus_scale_benchmark.py `
  --workspace C:\Users\shich\MEMORY\scratch\vector-lake-stability-20260822 `
  --nodes 12000 --workers 8 --fail-on-slo
```

完整 SLO、问题编号和证据边界见 `docs/corpus-scale-stability-plan.md`。
