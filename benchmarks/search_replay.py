#!/usr/bin/env python3
"""Replay a fixed query set through the retrieval path, and compare two runs.

The A1/A2 ranking work changes which candidates reach ``top_k``, and this lake keeps no record
that could show whether a change improved retrieval or damaged it: there is no ``log.md`` and no
query table.  ``search_ledger`` now records production answers, but only as digests, which answers
"is the same query answering differently" and not "is the answer better".

This harness supplies the other half: a committed query set, a deterministic replay, and a diff of
two runs.  It is an A/B instrument, not a benchmark -- most queries are deliberately unlabelled,
and the metric block says how many were scored.

Vector source (the vector path needs a query embedding, which is a provider round trip):

    snapshot  (default) a JSON file under ``<meta>/runtime/``; created on first run
    live      call the provider and refresh the snapshot
    stored    one stored vector for every query -- offline, deterministic, and *not* a measure of
              retrieval quality; it exercises the fusion mechanics only

Usage:

    python benchmarks/search_replay.py --out /tmp/sum.json
    VECTOR_LAKE_FUSION=rrf python benchmarks/search_replay.py --out /tmp/rrf.json
    python benchmarks/search_replay.py --compare /tmp/sum.json /tmp/rrf.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import struct
import sys
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from vector_lake import db_store, tool_search  # noqa: E402
from vector_lake.wiki_utils import get_meta_dir  # noqa: E402

try:  # absent before the ledger landed; the harness must also run against an older tree
    from vector_lake import search_ledger  # noqa: F811
except ImportError:  # pragma: no cover - only reachable when replaying an older checkout
    search_ledger = None


def query_digest(query: str) -> str:
    """Prefers the ledger's digest so a replay keys match production entries."""
    if search_ledger is not None:
        return search_ledger.query_digest(query)
    import hashlib

    return hashlib.sha256(str(query).encode("utf-8")).hexdigest()[:16]

QUERY_SET = REPO / "benchmarks" / "search_eval_queries.jsonl"
SNAPSHOT_NAME = "search_eval_vectors.json"


def load_queries(path: pathlib.Path, limit: int = 0) -> list[dict]:
    queries = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(json.loads(line))
    return queries[:limit] if limit else queries


def snapshot_path() -> pathlib.Path:
    return get_meta_dir() / "runtime" / SNAPSHOT_NAME


def _stored_vector() -> list[float]:
    """One row of the vector projection, chosen deterministically, for the offline mode."""
    conn = db_store.get_connection()
    row = conn.execute("select embedding from vec_embeddings order by entity_id limit 1").fetchone()
    if row is None:
        raise SystemExit("vec_embeddings is empty; use --vectors live or run embedding-backfill")
    blob = row["embedding"]
    dim = len(blob) // 4
    return list(struct.unpack(f"<{dim}f", blob[: dim * 4]))


def build_vectors(queries: list[dict], mode: str) -> dict[str, list[float]]:
    """``{query_digest: vector}`` for every query in the set, per ``mode``."""
    path = snapshot_path()
    if mode == "stored":
        vector = _stored_vector()
        return {query_digest(item["query"]): vector for item in queries}

    cached: dict[str, list[float]] = {}
    if mode == "snapshot" and path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))

    missing = [
        item["query"]
        for item in queries
        if query_digest(item["query"]) not in cached
    ]
    if mode == "live" or missing:
        if missing and mode == "snapshot":
            print(f"[vectors] {len(missing)} quer{'y' if len(missing) == 1 else 'ies'} not in the "
                  f"snapshot; fetching from the provider", file=sys.stderr)
        for query in missing:
            vector, reason = tool_search._get_query_embedding(query)
            if not vector:
                print(f"[vectors] unavailable for one query: {reason}; using a stored vector",
                      file=sys.stderr)
                vector = _stored_vector()
            cached[query_digest(query)] = vector
        if mode in {"snapshot", "live"}:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached), encoding="utf-8")
            print(f"[vectors] snapshot written: {path} ({len(cached)} vectors)", file=sys.stderr)

    return cached


def run(queries: list[dict], vectors: dict[str, list[float]], top_k: int) -> dict:
    results = {}
    for item in queries:
        query = item["query"]
        digest = query_digest(query)
        vector = vectors.get(digest) or []
        with mock.patch.object(tool_search, "_get_query_embedding", lambda q, v=vector: (v, None)):
            final, notes, error = tool_search._search_scored_pages(query, top_k=top_k)
        results[digest] = {
            "query_chars": len(query),
            "expect": item.get("expect") or [],
            "returned": [
                {"key": node["_key"], "origin": node.get("_origin", "?"), "score": round(score, 6)}
                for score, node in final
            ],
            "notes": list(notes or []),
            "error": error,
        }
    return results


def metrics(results: dict) -> dict:
    labelled = {k: v for k, v in results.items() if v["expect"]}
    hits = 0
    reciprocal = 0.0
    for row in labelled.values():
        keys = [entry["key"] for entry in row["returned"]]
        positions = [keys.index(key) + 1 for key in row["expect"] if key in keys]
        if positions:
            hits += 1
            reciprocal += 1.0 / min(positions)
    origins: dict[str, int] = {}
    for row in results.values():
        for entry in row["returned"]:
            origins[entry["origin"]] = origins.get(entry["origin"], 0) + 1
    return {
        "queries": len(results),
        "labelled": len(labelled),
        "recall_at_k": round(hits / len(labelled), 4) if labelled else None,
        "mrr": round(reciprocal / len(labelled), 4) if labelled else None,
        "origins": dict(sorted(origins.items())),
        "errors": sum(1 for row in results.values() if row["error"]),
    }


def compare(left_path: str, right_path: str) -> int:
    left = json.loads(pathlib.Path(left_path).read_text(encoding="utf-8"))
    right = json.loads(pathlib.Path(right_path).read_text(encoding="utf-8"))
    left_rows, right_rows = left["results"], right["results"]
    changed = []
    for digest, row in left_rows.items():
        other = right_rows.get(digest)
        if other is None:
            continue
        before = [entry["key"] for entry in row["returned"]]
        after = [entry["key"] for entry in other["returned"]]
        if before != after:
            changed.append((row.get("query_chars"), before, after))

    print(f"left : {left_path}  {json.dumps(left['metrics'], sort_keys=True)}")
    print(f"right: {right_path}  {json.dumps(right['metrics'], sort_keys=True)}")
    print(f"queries with a different ordering: {len(changed)}/{len(left_rows)}")
    for chars, before, after in changed:
        print(f"\n  query({chars} chars)")
        print(f"    before: {before}")
        print(f"    after : {after}")
    return 1 if changed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queries", default=str(QUERY_SET))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--vectors", choices=("snapshot", "live", "stored"), default="snapshot")
    parser.add_argument("--out", default="")
    parser.add_argument("--compare", nargs=2, metavar=("LEFT", "RIGHT"), default=None)
    args = parser.parse_args()

    if args.compare:
        return compare(*args.compare)

    # Evaluation runs must not be mixed into the production ledger, where they would look like
    # operator queries and inflate the distinct-query count.
    os.environ.setdefault("VECTOR_LAKE_SEARCH_LEDGER", "0")

    queries = load_queries(pathlib.Path(args.queries), args.limit)
    vectors = build_vectors(queries, args.vectors)
    results = run(queries, vectors, args.top_k)
    payload = {
        "config": {
            "fusion": os.environ.get("VECTOR_LAKE_FUSION", "sum"),
            "expansion_quota": os.environ.get("VECTOR_LAKE_EXPANSION_QUOTA", ""),
            "top_k": args.top_k,
            "vectors": args.vectors,
        },
        "metrics": metrics(results),
        "results": results,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    print(json.dumps(payload["config"], sort_keys=True), file=sys.stderr)
    print(json.dumps(payload["metrics"], sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
