"""Real-data audit of the search path: implementation stages, effect, failure modes.

Everything here runs against the live corpus (7,164 pages) and the judged r3 query set, with the
vector arm frozen from the replay snapshot so the embedding provider's rate limit cannot change the
subject mid-audit.  Read-only.
"""

import collections
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarks import search_replay as sr  # noqa: E402
from vector_lake import tool_search  # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 100
TOP_K = 5
WINDOW = 20

# --- freeze the vector arm ----------------------------------------------------------------
snapshot = json.loads(sr.snapshot_path().read_text(encoding="utf-8"))
missing = []


def _frozen(query: str):
    vector = snapshot.get(sr.query_digest(query))
    if vector is None:
        missing.append(query)
        return [], "not in snapshot"
    return vector, "snapshot"


tool_search._get_query_embedding = _frozen

rows = []
for line in Path("benchmarks/search_eval_labels_r3_p.jsonl").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    try:
        item = json.loads(line)
    except ValueError:
        continue
    if item.get("query") and item.get("relevant"):
        rows.append(item)
rows = rows[:LIMIT]
print(f"查询 {len(rows)}（r3 标注集，冻结向量）\n")

# --- B. per-arm contribution and where recall is lost --------------------------------------
origin_counts = collections.Counter()
place = collections.Counter()
short_windows = 0
zero_scores = 0
ties_at_zero = 0
prefix_breaks = 0
repeat_breaks = 0

for item in rows:
    query = item["query"]
    relevant = set(item["relevant"])
    wide, _notes, _f = tool_search._search_scored_pages(query, WINDOW * 2)  # ~= the 40-candidate pool
    narrow, notes, _f = tool_search._search_scored_pages(query, TOP_K)
    wide_keys = [n["_key"] for _s, n in wide]
    narrow_keys = [n["_key"] for _s, n in narrow]
    if len(narrow) < TOP_K:
        short_windows += 1
    zero_scores += sum(1 for score, _n in wide if score == 0.0)
    top = max((s for s, _n in wide), default=0.0)
    ties_at_zero += sum(1 for s, _n in wide if s == 0.0) and 1 or 0
    for _s, node in wide:
        origin_counts[node.get("_origin", "?")] += 1
    for key in relevant:
        if key in narrow_keys:
            place["in top-5"] += 1
        elif key in wide_keys:
            place["in pool, ranked out"] += 1
        else:
            place["not retrieved"] += 1
    # invariant: the top-5 is the prefix of a larger window
    if narrow_keys != wide_keys[: len(narrow_keys)]:
        prefix_breaks += 1
    # determinism: the same query twice
    again, _n, _f = tool_search._search_scored_pages(query, TOP_K)
    if [n["_key"] for _s, n in again] != narrow_keys:
        repeat_breaks += 1

total_relevant = sum(len(set(item["relevant"])) for item in rows)
print("== 每臂贡献（进入 40 候选池的 7,{0} 条候选的来源）".format(0))
for origin, count in origin_counts.most_common():
    print(f"   {origin:<8} {count:>6}  ({count / max(1, sum(origin_counts.values())) * 100:.1f}%)")
print(f"\n== 相关页的去向（{total_relevant} 条标注相关页）")
for place_name, count in place.most_common():
    print(f"   {place_name:<20} {count:>5}  ({count / max(1, total_relevant) * 100:.1f}%)")

print("\n== 排序不变量与信号")
print(f"   top-5 不是更大窗口前缀的查询数: {prefix_breaks}/{len(rows)}")
print(f"   重复调用结果不同的查询数:     {repeat_breaks}/{len(rows)}")
print(f"   返回不满 {TOP_K} 条的查询数:        {short_windows}/{len(rows)}")
print(f"   池内分数恰为 0.0 的候选:       {zero_scores} 条（其中至少一条的查询数 {ties_at_zero}）")

# --- C. latency -----------------------------------------------------------------------------
latencies, fts_times = [], []
for item in rows:
    query = item["query"]
    start = time.perf_counter()
    tool_search._search_scored_pages(query, TOP_K)
    latencies.append(time.perf_counter() - start)
    start = time.perf_counter()
    tool_search._get_fts_search_results(query, 25)
    fts_times.append(time.perf_counter() - start)


def pct(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


print("\n== 延迟（同进程热态，秒）")
print(f"   检索 p50 {pct(latencies, 0.5) * 1000:7.1f}ms  p95 {pct(latencies, 0.95) * 1000:7.1f}ms  最大 {max(latencies) * 1000:7.1f}ms")
print(f"   FTS 臂 p50 {pct(fts_times, 0.5) * 1000:7.1f}ms  p95 {pct(fts_times, 0.95) * 1000:7.1f}ms")

# --- D. edges and degradation ----------------------------------------------------------------
print("\n== 边界与降级")
edges = [
    ("", "空查询"),
    ("医", "单字"),
    ("zzzzqqqq", "不存在的词"),
    ("Epic", "英文实体"),
    ("信创 " * 40, "超长重复"),
    ("医疗 数字化", "普通两词"),
]
for query, label in edges:
    try:
        start = time.perf_counter()
        scored, notes, _f = tool_search._search_scored_pages(query, TOP_K)
        elapsed = (time.perf_counter() - start) * 1000
        keys = [n["_key"] for _s, n in scored][:2]
        print(f"   {label:<10} 返回 {len(scored):>2} 条 {elapsed:6.0f}ms  首位 {keys[:1]} notes={notes[:1]}")
    except Exception as exc:  # noqa: BLE001 - an audit wants the exception class, not a crash
        print(f"   {label:<10} 抛出 {type(exc).__name__}: {str(exc)[:70]}")

# the vector arm unavailable (a provider failure) must be reported, not silently narrowed
tool_search._get_query_embedding = lambda q: ([], "simulated provider failure")
scored, notes, _f = tool_search._search_scored_pages("信创 医院", TOP_K)
print(f"   向量臂不可用  返回 {len(scored)} 条 notes={notes[:1]}")
if missing:
    print(f"   （快照缺失的查询 {len(missing)} 条，已按不可用处理）")
