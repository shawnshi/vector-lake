"""Second pass: attribute the per-query cost, check the vector arm's precondition, and say what
beats the relevant pages that made it into the pool but not into the window.
"""

import collections
import json
import math
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarks import search_replay as sr  # noqa: E402
from vector_lake import db_store, tool_search  # noqa: E402

snapshot = json.loads(sr.snapshot_path().read_text(encoding="utf-8"))
tool_search._get_query_embedding = lambda q: (snapshot.get(sr.query_digest(q)) or [], "snapshot")

rows = []
for line in Path("benchmarks/search_eval_labels_r3_p.jsonl").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    item = json.loads(line)
    if item.get("query") and item.get("relevant"):
        rows.append(item)
rows = rows[:30]

# --- 1. the vector arm's precondition: cosine from L2 only holds for unit vectors ------------
connection = db_store.get_connection()
norms = []
for row in connection.execute("SELECT embedding FROM vec_embeddings LIMIT 500"):
    blob = row[0]
    dim = len(blob) // 4
    vector = struct.unpack(f"<{dim}f", blob[: dim * 4])
    norms.append(math.sqrt(sum(value * value for value in vector)))
print(f"向量范数（500 条抽样）: min {min(norms):.4f}  中位 {sorted(norms)[len(norms) // 2]:.4f}  最大 {max(norms):.4f}")
print(f"  归一化到 1±1e-3 的比例: {sum(1 for v in norms if abs(v - 1) < 1e-3) / len(norms):.1%}\n")

# --- 2. cost attribution ---------------------------------------------------------------------
timings = collections.defaultdict(list)
graph_fn = getattr(tool_search, "_get_ppr_search_results", None) or getattr(
    tool_search, "_get_graph_expansion_results", None
)
for item in rows:
    query = item["query"]
    start = time.perf_counter()
    tool_search._search_scored_pages(query, 5)
    timings["whole search"].append(time.perf_counter() - start)

    vector = snapshot.get(sr.query_digest(query)) or []
    start = time.perf_counter()
    tool_search._get_fts_search_results(query, 25)
    timings["FTS arm"].append(time.perf_counter() - start)

    start = time.perf_counter()
    tool_search._get_vector_search_results(vector, 25)
    timings["vector arm"].append(time.perf_counter() - start)

    wide, _n, _f = tool_search._search_scored_pages(query, 40)
    start = time.perf_counter()
    tool_search._rerank_candidates_locally(query, wide)
    timings["Phase-2 rerank"].append(time.perf_counter() - start)

    start = time.perf_counter()
    tool_search._rerank_candidates_locally(query, [])
    timings["rerank (no candidates)"].append(time.perf_counter() - start)

print("== 单查询成本（热态，均值/中位，毫秒）")
for name, values in timings.items():
    print(f"   {name:<22} 均值 {sum(values) / len(values) * 1000:7.1f}  中位 {sorted(values)[len(values) // 2] * 1000:7.1f}")
if graph_fn:
    print(f"   （图扩展函数 {graph_fn.__name__} 未单独计时）")
else:
    print("   （未找到图扩展入口，PRR 计入整体）")

# --- 3. what beats a relevant page that did make it into the pool ----------------------------
beaten = []
for item in rows:
    relevant = set(item["relevant"])
    wide, _n, _f = tool_search._search_scored_pages(item["query"], 40)
    keys = [n["_key"] for _s, n in wide]
    scores = {n["_key"]: (s, n) for s, n in wide}
    in_window = set(keys[:5])
    for key in relevant:
        if key in scores and key not in in_window:
            cut = scores[keys[4]][0] if len(keys) >= 5 else 0.0
            page_score, node = scores[key]
            above = [k for k in keys[:5]]
            beaten.append(
                {
                    "query": item["query"],
                    "page": key,
                    "type": node.get("type"),
                    "score": page_score,
                    "fifth": cut,
                    "above": above,
                }
            )
print(f"\n== 进入 40 池但未进 top-5 的相关页: {len(beaten)} 条")
type_counts = collections.Counter(item["type"] for item in beaten)
print(f"   它们的页面类型分布: {dict(type_counts)}")
gap = [item["fifth"] - item["score"] for item in beaten]
if gap:
    print(f"   与第 5 名的分差: 中位 {sorted(gap)[len(gap) // 2]:.3f}  最大 {max(gap):.3f}（0 = 同分被稳定序压后）")
for item in beaten[:6]:
    print(f"   {item['page'][:44]:<44} type={str(item['type']):<9} 分 {item['score']:.3f} vs 第5名 {item['fifth']:.3f}")
    print(f"      查询: {item['query'][:56]}")
    print(f"      压过它的: {[k[:34] for k in item['above'][:3]]}")
