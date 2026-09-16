"""Read-only prototype of the FTS5 operational-memory retriever.

Builds a scratch FTS5 projection of ``operational_memory_index`` (CJK characters
are space-separated so an FTS5 phrase query is an exact substring test) and
measures build cost, size, query latency and recall against the exact
``legacy`` full-scan oracle.  Nothing here touches the live database.

Run::

    VECTOR_LAKE_MEMORY_DIR=<MEMORY> \
        python benchmarks/probe_fts_retriever.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

SHORT_QUERIES = [
    "信创 医院 HIS 集成平台",
    "卫宁健康 DRG",
    "preference deployment_target",
    "电子病历六级",
]
LONG_QUERIES = [
    "公立医院高质量发展 电子病历六级 互联互通五乙 智慧服务分级评估",
    "DRG DIP 医保支付方式改革 对公立医院运营管理的影响",
    "人工智能 大模型 在医院 临床 场景 的 落地 路径 与 风险",
]


def to_indexed(text: str) -> str:
    """Space-separate CJK characters so each is a token and phrases are substrings."""
    if not CJK.search(text):
        return text
    out: list[str] = []
    for ch in text:
        if CJK.match(ch):
            out.append(" ")
            out.append(ch)
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def to_match_query(terms: list[str]) -> str:
    parts = []
    for term in terms:
        if not term:
            continue
        if CJK.search(term):
            parts.append('"' + " ".join(to_indexed(term).split()) + '"')
        else:
            parts.append('"' + term.replace('"', '""') + '"')
    return " OR ".join(parts)


def build(path: Path) -> tuple[float, float]:
    from vector_lake.db_store import get_connection

    if path.exists():
        path.unlink()
    scratch = sqlite3.connect(str(path))
    scratch.execute(
        "CREATE VIRTUAL TABLE om_fts USING fts5("
        "key_blob, text_blob, page_blob, type_blob, "
        "content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 1')"
    )
    source = get_connection()
    rows = source.execute(
        "SELECT rowid, memory_type, key_blob, text_blob, page_blob FROM operational_memory_index"
    ).fetchall()
    started = time.perf_counter()
    with scratch:
        scratch.executemany(
            "INSERT INTO om_fts(rowid, key_blob, text_blob, page_blob, type_blob) VALUES (?,?,?,?,?)",
            [
                (row[0], to_indexed(row[2]), to_indexed(row[3]), to_indexed(row[4]), str(row[1]))
                for row in rows
            ],
        )
    elapsed = time.perf_counter() - started
    size_mb = path.stat().st_size / 1048576
    return elapsed, size_mb


def main() -> int:
    from vector_lake import governance_store
    from vector_lake.db_store import get_connection

    path = REPO_ROOT / "tmp" / "bench" / "fts_probe.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    elapsed, size_mb = build(path)
    print(f"build: {elapsed:.1f}s, {size_mb:.0f} MB (contentless FTS5)")

    scratch = sqlite3.connect(str(path))
    from vector_lake.db_store import get_db_path

    scratch.execute("ATTACH DATABASE ? AS main_index", (str(get_db_path()),))
    source = get_connection()
    hidden = ("archived", "expired", "superseded")

    def fts_ids(terms, limit, include_history=False, allowed_types=None):
        match = to_match_query(terms)
        if not match:
            return []
        sql = (
            "SELECT i.memory_id, bm25(om_fts, 4.0, 3.0, 1.0, 1.0) AS rank "
            "FROM om_fts JOIN main_index.operational_memory_index AS i ON i.rowid = om_fts.rowid "
            "WHERE om_fts MATCH ?"
        )
        params: list = [match]
        if not include_history:
            sql += " AND i.validity_state NOT IN (%s)" % ",".join("?" * len(hidden))
            params.extend(hidden)
        if allowed_types:
            sql += " AND i.memory_type IN (%s)" % ",".join("?" * len(sorted(allowed_types)))
            params.extend(sorted(allowed_types))
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return [str(row[0]) for row in scratch.execute(sql, params)]

    for window in (48, 200):
        print(f"\n--- window={window} ---")
        for query in SHORT_QUERIES + LONG_QUERIES:
            terms = governance_store._query_terms(query)
            os.environ["VECTOR_LAKE_MEMORY_SEARCH"] = "legacy"
            legacy = [m["memory_id"] for m in governance_store.search_operational_memory(query, top_k=12)]

            samples = []
            for _ in range(3):
                started = time.perf_counter()
                candidate_ids = fts_ids(terms, window)
                samples.append(time.perf_counter() - started)
            hit = len(set(legacy) & set(candidate_ids))
            print(
                f"terms={len(terms):3d} fts={min(samples)*1000:7.2f}ms "
                f"cand={len(candidate_ids):4d} legacy_top12_recalled={hit}/12  {query[:34]}"
            )

    # exact re-score cost for a window of rows
    for window in (48, 200):
        terms = governance_store._query_terms(LONG_QUERIES[0])
        ids = fts_ids(terms, window)
        placeholders = ",".join("?" * len(ids))
        started = time.perf_counter()
        rows = source.execute(
            f"SELECT memory_id, key_blob, text_blob, page_blob, memory_type FROM operational_memory_index "
            f"WHERE memory_id IN ({placeholders})",
            ids,
        ).fetchall()
        fetched = time.perf_counter() - started
        started = time.perf_counter()
        for row in rows:
            relevance = 0
            for term in terms:
                relevance += 4 * (term in row["key_blob"]) + 3 * (term in row["text_blob"]) + (term in row["page_blob"])
                relevance += term in row["memory_type"]
        rescored = time.perf_counter() - started
        print(f"window={window:4d}: fetch {fetched*1000:.1f}ms + python exact rescore {rescored*1000:.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
