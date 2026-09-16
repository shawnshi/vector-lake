"""Hot-path benchmark for the read surfaces that must stay sub-second at wiki scale.

Covers the three operator-facing read paths named in the performance contract:
``search`` (``search_vector_lake``), ``query`` (``assemble_context`` /
``build_memory_packet``) and ``timeline`` (``search_timeline_events``), plus the
operational-memory retriever they all funnel through.

Usage::

    VECTOR_LAKE_MEMORY_DIR=<MEMORY> \
        python benchmarks/bench_hot_paths.py --label before --repeat 5

Set ``--no-embedding`` to unset ``GEMINI_API_KEY`` for the run, which isolates
the local cost from the remote embedding provider.  Without it, the vector half
of the hybrid retrieval is measured too.  Results are printed as a table and
written to ``tmp/bench/bench_<label>.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

QUERIES = [
    "信创 医院 HIS 集成平台",
    "电子病历六级 评级 评审",
    "DRG DIP 医保支付 合规",
    "AI 原生医院 多智能体",
]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _timed(fn, repeats: int) -> tuple[list[float], object]:
    samples: list[float] = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = fn()
        samples.append(time.perf_counter() - started)
    return samples, result


def build_cases():
    from vector_lake import tool_search, tool_timeline
    from vector_lake import governance_store

    query = QUERIES[0]
    timeline_entity = "HIS"

    return [
        ("operational_memory_search", lambda: governance_store.search_operational_memory(query, top_k=12)),
        ("build_memory_packet", lambda: tool_search.build_memory_packet(query)),
        ("assemble_context", lambda: tool_search.assemble_context(query)),
        ("search_vector_lake[page]", lambda: tool_search.search_vector_lake(query, top_k=5)),
        ("search_vector_lake[memory]", lambda: tool_search.search_vector_lake(query, top_k=5, mode="memory")),
        ("search_timeline_events", lambda: tool_timeline.search_timeline_events(entity_name=timeline_entity, limit=10)),
    ]


def corpus_facts() -> dict:
    from vector_lake.db_store import get_connection
    from vector_lake.wiki_utils import get_memory_dir, get_wiki_dir

    wiki_dir = get_wiki_dir()
    facts = {
        "memory_root": str(get_memory_dir()),
        "wiki_pages": len([p for p in wiki_dir.glob("*.md")]),
        "index_json_mb": round((wiki_dir / "index.json").stat().st_size / 1048576, 1)
        if (wiki_dir / "index.json").exists()
        else 0.0,
    }
    try:
        conn = get_connection()
        for label, sql in (
            ("claims", "SELECT COUNT(*) FROM claims"),
            ("operational_memory", "SELECT COUNT(*) FROM operational_memory"),
            ("timeline_events", "SELECT COUNT(*) FROM timeline_events"),
        ):
            facts[label] = conn.execute(sql).fetchone()[0]
    except Exception as exc:  # pragma: no cover - diagnostics only
        facts["db_error"] = f"{type(exc).__name__}: {exc}"
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="run", help="artifact label, e.g. before/after")
    parser.add_argument("--repeat", type=int, default=3, help="timed repetitions per case")
    parser.add_argument("--warmup", type=int, default=1, help="untimed warmup repetitions")
    parser.add_argument(
        "--no-embedding",
        action="store_true",
        help="unset GEMINI_API_KEY so only local retrieval cost is measured",
    )
    parser.add_argument("--out", default=None, help="artifact path override")
    args = parser.parse_args()

    if args.no_embedding:
        os.environ.pop("GEMINI_API_KEY", None)
    embedding_active = bool(os.environ.get("GEMINI_API_KEY"))

    facts = corpus_facts()
    print("corpus:", json.dumps(facts, ensure_ascii=False))
    print(f"embedding provider active: {embedding_active}")

    results = {}
    for name, fn in build_cases():
        try:
            for _ in range(args.warmup):
                fn()
            samples, _ = _timed(fn, args.repeat)
        except Exception as exc:  # pragma: no cover - diagnostics only
            print(f"{name:28s} ERROR {type(exc).__name__}: {exc}")
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        entry = {
            "p50": round(statistics.median(samples), 3),
            "min": round(min(samples), 3),
            "max": round(max(samples), 3),
            "p95": round(_percentile(samples, 0.95), 3),
            "samples": [round(value, 3) for value in samples],
        }
        results[name] = entry
        print(f"{name:28s} p50={entry['p50']:7.3f}s min={entry['min']:7.3f}s max={entry['max']:7.3f}s")

    artifact = {
        "label": args.label,
        "embedding_provider_active": embedding_active,
        "repeat": args.repeat,
        "warmup": args.warmup,
        "corpus": facts,
        "results": results,
    }
    out_path = Path(args.out) if args.out else REPO_ROOT / "tmp" / "bench" / f"bench_{args.label}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print("artifact:", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
