"""P7 verification: does a source preference shorten the result window?

The complaint that motivated the fixed penalty: with the old window cap
``max(1, int(top_k * 0.6))``, top_k=20 returned fewer than 20 rows for 104 of 333 queries (31%),
because a per-answer threshold on *content type* scales with the answer size.  This measures the
window under the current implementation at three penalty settings.
"""

import collections
import json
import os
from pathlib import Path

from vector_lake import tool_search

MEM = Path.home() / "MEMORY"
LABELS = Path("benchmarks/search_eval_labels_r3_p.jsonl")

queries = []
with open(LABELS, encoding="utf-8") as handle:
    for line in handle:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        query = payload.get("query") or payload.get("q")
        if query:
            queries.append(str(query))
print(f"queries: {len(queries)}")

for setting in ("0.6", "0.0", "1.0"):
    os.environ["VECTOR_LAKE_SOURCE_RANK_PENALTY"] = setting
    short = collections.Counter()
    sources_in_window = collections.Counter()
    for query in queries:
        scored, _notes, _filtered = tool_search._search_scored_pages(query, 20)
        short[len(scored)] += 1
        sources_in_window[sum(1 for _s, n in scored if n.get("type", "").lower() == "source")] += 1
    full = short.get(20, 0)
    under = sum(count for length, count in short.items() if length < 20)
    print(
        f"penalty={setting}: 满窗 {full}/{len(queries)}，不满窗 {under}，"
        f"窗口长度分布 {sorted(short.items())[:5]}"
    )
    print(f"    窗口内来源类条数分布 {sorted(sources_in_window.items())[:6]} (max 20)")
