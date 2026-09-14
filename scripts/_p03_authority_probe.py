"""Read-only: which side of the claim/page graph dual-write is authoritative?

Both tables hold the same logical edge set and readers UNION them, but the repo's
own audit declares they "must be semantically equal". 107 rows differ. Before
repairing anything, decide which side is stale -- by checking each divergent edge
against the canonical source entity's own recorded relations, which is the
authoritative content both tables are supposedly derived from.

Read-only. Opens the database with mode=ro.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter

DB = os.path.expanduser("~/MEMORY/wiki/.meta/vector_lake.db")
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

claim = set(
    conn.execute("SELECT source_id, target_id, relation FROM claim_graph_edges")
)
page = set(
    conn.execute("SELECT source_id, target_id, relation FROM page_graph_edges")
)
only_page = page - claim
only_claim = claim - page
print(f"claim_graph_edges={len(claim):,}  page_graph_edges={len(page):,}")
print(f"only_page={len(only_page)}  only_claim={len(only_claim)}")


def entity_relations(page_key: str) -> tuple[set[tuple[str, str]], bool]:
    """(relation pairs recorded on the entity, found?) from canonical data_json."""
    row = conn.execute(
        "SELECT entity_id, data_json FROM entities "
        "WHERE json_extract(data_json, '$.page_key') = ? LIMIT 1",
        (page_key,),
    ).fetchone()
    if row is None:
        return set(), False
    try:
        data = json.loads(row[1]) if row[1] else {}
    except (TypeError, ValueError):
        return set(), True
    pairs: set[tuple[str, str]] = set()
    for key in ("relations", "triples", "links", "outbound_links"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                target = item.get("target") or item.get("target_id")
                pred = item.get("predicate") or item.get("relation")
                if target and pred:
                    pairs.add((str(target), str(pred)))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.add((str(item[0]), str(item[1])))
    return pairs, True


def classify(edges: set[tuple[str, str, str]], label: str) -> Counter:
    """Is each divergent edge backed by the source entity's canonical relations?"""
    stats: Counter = Counter()
    for source, target, relation in sorted(edges):
        pairs, found = entity_relations(source)
        if not found:
            stats["source_entity_absent"] += 1
        elif (target, relation) in pairs:
            stats["backed_by_canonical"] += 1
        else:
            stats["not_backed_by_canonical"] += 1
    print(f"\n{label} ({len(edges)} rows):")
    for key, value in stats.most_common():
        print(f"  {key}: {value}")
    return stats


print("\n=== only_page: rows present only in page_graph_edges ===")
only_page_stats = classify(only_page, "only_page")
print("\n=== only_claim: rows present only in claim_graph_edges ===")
only_claim_stats = classify(only_claim, "only_claim")

print("\n=== 判读 ===")
print(
    "If only_page is mostly 'not_backed_by_canonical', page_graph_edges holds stale "
    "edges and claim_graph_edges is the side that tracks canonical content."
)
print(
    "If only_claim is mostly 'not_backed_by_canonical', the reverse holds."
)
