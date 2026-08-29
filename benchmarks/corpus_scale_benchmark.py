"""Repeatable 10k+ corpus latency and stability benchmark for Vector Lake.

The fixture is synthetic, local-only, and removed when the run ends.  It
exercises the real FTS query, exact-identity, graph expansion, ranking, result
budget, index decoder/cache, and multi-threaded connection paths without
calling an embedding provider or mutating a real MEMORY root.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vector_lake import db_store, index_snapshot, indexer, tool_search  # noqa: E402
from vector_lake.projection_format_v2 import (  # noqa: E402
    build_projection_roots,
    publish_prepared_projection,
)
from vector_lake.search_projection_contract import fts_corpus_sha256  # noqa: E402


CONTRACT_VERSION = "vector-lake-corpus-scale-benchmark/v1"
DEFAULT_NODE_COUNT = 12_000
DEFAULT_SERIAL_QUERIES = 120
DEFAULT_CONCURRENT_QUERIES = 320
DEFAULT_WORKERS = 8
DEFAULT_SLOS = {
    "minimum_node_count": 10_000,
    "cold_index_load_ms": 2_500.0,
    "warm_index_load_p95_ms": 25.0,
    "serial_search_p95_ms": 250.0,
    "serial_search_max_ms": 1_000.0,
    "concurrent_search_p95_ms": 750.0,
    "fts_fallback_p95_ms": 750.0,
    "minimum_concurrent_qps": 20.0,
    "maximum_error_rate": 0.0,
    "maximum_search_rss_delta_mib": 512.0,
    "identity_cold_build_passes": 1,
}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(value, 6)


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "min_ms": round(min(values), 6) if values else 0.0,
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": round(max(values), 6) if values else 0.0,
        "mean_ms": round(statistics.fmean(values), 6) if values else 0.0,
    }


def _working_set_bytes() -> int | None:
    if os.name != "nt":
        try:
            import resource

            value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            return value * (1 if sys.platform == "darwin" else 1024)
        except (ImportError, OSError, ValueError):
            return None
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            counters.cb,
        )
        return int(counters.WorkingSetSize) if ok else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _node(index: int) -> tuple[str, dict, tuple[str, str, str, str]]:
    suffix = f"{index:05d}"
    topic = f"topic{index % 100:03d}"
    key = f"Concept_Document-{suffix}"
    title = f"Synthetic Document {suffix}"
    summary = f"Synthetic stability knowledge {topic} needle{suffix}"
    node = {
        "id": f"entity-{suffix}",
        "title": title,
        "aliases": [f"Synthetic Alias {suffix}"],
        "summary": summary,
        "type": "concept",
        "status": "Active",
        "domain": "synthetic",
        "topic_cluster": f"cluster-{index % 16:02d}",
        "updated_at": "2026-08-22T00:00:00+00:00",
    }
    fts_row = (key, title, summary, f"{summary} repeatable retrieval corpus")
    return key, node, fts_row


def _build_fixture(root: Path, node_count: int) -> dict:
    started = time.perf_counter()
    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    nodes = {}
    fts_rows = []
    edges = []
    for index in range(node_count):
        key, node, fts_row = _node(index)
        nodes[key] = node
        fts_rows.append(fts_row)
        if index:
            edges.append(
                {
                    "source": f"Concept_Document-{index - 1:05d}",
                    "target": key,
                    "weight": 1.0,
                }
            )
    snapshot_payload = {
        "graph_state": {"dirty": False},
        "nodes": nodes,
        "weighted_edges": edges,
    }
    db_path = root / "vector_lake_benchmark.db"
    if db_store.get_db_path().resolve() != db_path.resolve():
        raise RuntimeError("benchmark database must be isolated inside its fixture")
    db_store.init_db()
    canonical_generation = indexer.canonical_runtime_generation_snapshot()
    prepared = build_projection_roots(
        wiki_dir,
        snapshot_payload,
        {"nodes": [], "edges": [], "schema_version": "1.0"},
        canonical_generation=canonical_generation,
        published_at_utc="2026-08-22T00:00:00+00:00",
    )

    def commit_search_projection(connection, candidate):
        return db_store.apply_search_projection_mutations(
            connection,
            upserts=fts_rows,
            reset_search=True,
            projection_generation=candidate.projection_generation,
            canonical_generation=candidate.canonical_generation,
            expected_row_count=node_count,
            expected_corpus_sha256=fts_corpus_sha256(fts_rows),
        )

    publish_prepared_projection(
        wiki_dir,
        prepared,
        transaction_mutation=commit_search_projection,
    )
    return {
        "index_path": wiki_dir / "index.json",
        "wiki_dir": wiki_dir,
        "db_path": db_path,
        "build_ms": (time.perf_counter() - started) * 1000.0,
    }


class _CountingNodes:
    def __init__(self, nodes):
        self._nodes = nodes
        self._lock = threading.Lock()
        self.items_calls = 0

    def items(self):
        with self._lock:
            self.items_calls += 1
        return self._nodes.items()


def _reset_identity_cache() -> None:
    with tool_search._IDENTITY_LOOKUP_LOCK:
        tool_search._IDENTITY_LOOKUP_CACHE["signature"] = ""
        tool_search._IDENTITY_LOOKUP_CACHE["lookup"] = {}


def _measure_identity_cold_concurrency(snapshot: dict, workers: int) -> dict:
    counting_nodes = _CountingNodes(snapshot["nodes"])
    diagnostic_snapshot = {
        "projection_manifest": {
            "generation": f"identity-cold-{time.monotonic_ns()}"
        },
        "nodes": counting_nodes,
    }
    _reset_identity_cache()
    barrier = threading.Barrier(workers)

    def resolve(worker: int) -> float:
        barrier.wait(timeout=10.0)
        started = time.perf_counter()
        result = tool_search._exact_identity_scores(
            diagnostic_snapshot,
            f"Concept_Document-{worker % len(snapshot['nodes']):05d}",
        )
        if not result:
            raise RuntimeError("cold exact identity lookup returned no match")
        return (time.perf_counter() - started) * 1000.0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        latencies = list(executor.map(resolve, range(workers)))
    _reset_identity_cache()
    return {
        "build_passes": counting_nodes.items_calls,
        "latency": _latency_summary(latencies),
    }


def _workload(node_count: int, samples: int) -> list[tuple[str, str | None]]:
    cases = []
    for sample in range(samples):
        index = (sample * 7919) % node_count
        suffix = f"{index:05d}"
        variant = sample % 5
        if variant == 0:
            cases.append(
                (f"Concept_Document-{suffix}", f"Synthetic Document {suffix}")
            )
        elif variant == 1:
            cases.append((f"entity-{suffix}", f"Synthetic Document {suffix}"))
        elif variant == 2:
            cases.append(
                (f"Synthetic Alias {suffix}", f"Synthetic Document {suffix}")
            )
        elif variant == 3:
            cases.append((f"needle{suffix}", f"Synthetic Document {suffix}"))
        else:
            cases.append((f"topic{index % 100:03d}", None))
    return cases


def _query_once(query: str, expected_title: str | None) -> tuple[float, str | None]:
    started = time.perf_counter()
    try:
        result = tool_search.search_vector_lake(query, top_k=5)
        if "[Search degraded:" in result:
            return (time.perf_counter() - started) * 1000.0, "degraded"
        if expected_title and f"**{expected_title}**" not in result:
            return (time.perf_counter() - started) * 1000.0, "recall_miss"
        if len(result.encode("utf-8")) > tool_search._search_result_byte_limit():
            return (time.perf_counter() - started) * 1000.0, "result_budget"
        return (time.perf_counter() - started) * 1000.0, None
    except Exception as exc:
        return (time.perf_counter() - started) * 1000.0, type(exc).__name__


def _fallback_query_once(
    query: str,
    expected_title: str | None,
) -> tuple[float, str | None]:
    started = time.perf_counter()
    try:
        result = tool_search.search_vector_lake(query, top_k=5)
        if "[Search degraded: fts5]" not in result:
            return (time.perf_counter() - started) * 1000.0, "not_degraded"
        if expected_title and f"**{expected_title}**" not in result:
            return (time.perf_counter() - started) * 1000.0, "recall_miss"
        return (time.perf_counter() - started) * 1000.0, None
    except Exception as exc:
        return (time.perf_counter() - started) * 1000.0, type(exc).__name__


def _evaluate_slos(report: dict, slos: dict) -> dict:
    checks = {
        "minimum_node_count": report["corpus"]["node_count"]
        >= slos["minimum_node_count"],
        "cold_index_load_ms": report["index"]["cold_load_ms"]
        <= slos["cold_index_load_ms"],
        "warm_index_load_p95_ms": report["index"]["warm_load"]["p95_ms"]
        <= slos["warm_index_load_p95_ms"],
        "serial_search_p95_ms": report["search"]["serial"]["p95_ms"]
        <= slos["serial_search_p95_ms"],
        "serial_search_max_ms": report["search"]["serial"]["max_ms"]
        <= slos["serial_search_max_ms"],
        "concurrent_search_p95_ms": report["search"]["concurrent"]["p95_ms"]
        <= slos["concurrent_search_p95_ms"],
        "fts_fallback_p95_ms": (
            report["fts_fallback"]["latency"]["p95_ms"]
            <= slos["fts_fallback_p95_ms"]
            and report["fts_fallback"]["error_count"] == 0
        ),
        "minimum_concurrent_qps": report["search"]["concurrent_qps"]
        >= slos["minimum_concurrent_qps"],
        "maximum_error_rate": report["search"]["error_rate"]
        <= slos["maximum_error_rate"],
        "maximum_search_rss_delta_mib": (
            report["memory"]["search_rss_delta_mib"] is not None
            and report["memory"]["search_rss_delta_mib"]
            <= slos["maximum_search_rss_delta_mib"]
        ),
        "identity_cold_build_passes": report["identity_cold_concurrency"][
            "build_passes"
        ]
        <= slos["identity_cold_build_passes"],
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "thresholds": slos,
        "checks": checks,
        "failures": failures,
        "status": "pass" if not failures else "fail",
    }


def run_benchmark(
    workspace: str | os.PathLike,
    *,
    node_count: int = DEFAULT_NODE_COUNT,
    serial_queries: int = DEFAULT_SERIAL_QUERIES,
    concurrent_queries: int = DEFAULT_CONCURRENT_QUERIES,
    workers: int = DEFAULT_WORKERS,
    slos: dict | None = None,
) -> dict:
    if node_count < 1 or serial_queries < 1 or concurrent_queries < 1:
        raise ValueError("node and query counts must be positive")
    if not 1 <= workers <= 64:
        raise ValueError("workers must be between 1 and 64")
    workspace_path = Path(workspace).expanduser().resolve(strict=True)
    if not workspace_path.is_dir():
        raise ValueError("workspace must be a directory")
    effective_slos = {**DEFAULT_SLOS, **(slos or {})}

    with tempfile.TemporaryDirectory(
        prefix="vector-lake-corpus-scale-",
        dir=workspace_path,
    ) as fixture_dir:
        previous_db_path = os.environ.get("VECTOR_LAKE_DB_PATH")
        previous_embedding = os.environ.get("VECTOR_LAKE_QUERY_EMBEDDING")
        fixture_root = Path(fixture_dir)
        db_store.close_all_connections()
        os.environ["VECTOR_LAKE_DB_PATH"] = str(
            fixture_root / "vector_lake_benchmark.db"
        )
        os.environ["VECTOR_LAKE_QUERY_EMBEDDING"] = "0"
        try:
            fixture = _build_fixture(fixture_root, node_count)
            index_snapshot.clear_index_snapshot_cache_for_tests()
            cold_started = time.perf_counter()
            snapshot = index_snapshot.load_index_snapshot(fixture["index_path"])
            cold_load_ms = (time.perf_counter() - cold_started) * 1000.0
            warm_loads = []
            for _ in range(20):
                started = time.perf_counter()
                warm = index_snapshot.load_index_snapshot(fixture["index_path"])
                if warm is not snapshot:
                    raise RuntimeError(
                        "warm index cache did not preserve snapshot identity"
                    )
                warm_loads.append((time.perf_counter() - started) * 1000.0)

            identity_concurrency = _measure_identity_cold_concurrency(
                snapshot, workers
            )
            db_store.close_connection()
            rss_before = _working_set_bytes()
            serial_cases = _workload(node_count, serial_queries)
            concurrent_cases = _workload(node_count, concurrent_queries)
            serial_results = []
            concurrent_results = []
            fallback_results = []
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(tool_search, "get_index_path", lambda: fixture["index_path"])
                )
                stack.enter_context(
                    patch.object(tool_search, "get_wiki_dir", lambda: fixture["wiki_dir"])
                )
                stack.enter_context(
                    patch.object(tool_search, "_load_search_index", lambda _path: snapshot)
                )
                for query, expected in serial_cases:
                    serial_results.append(_query_once(query, expected))

                concurrent_started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [
                        executor.submit(_query_once, query, expected)
                        for query, expected in concurrent_cases
                    ]
                    for future in as_completed(futures):
                        concurrent_results.append(future.result())
                concurrent_elapsed = time.perf_counter() - concurrent_started
                normal_telemetry = tool_search.search_performance_status()["last"]

                fallback_cases = _workload(node_count, 20)
                tool_search._reset_search_backend_log_state()
                with patch.object(
                    tool_search,
                    "_get_fts_search_results",
                    side_effect=tool_search.SearchBackendError("fts5"),
                ):
                    for query, expected in fallback_cases:
                        fallback_results.append(
                            _fallback_query_once(query, expected)
                        )
                fallback_status = tool_search.search_performance_status()
                fallback_telemetry = fallback_status["last"]
                fallback_suppressed = fallback_status[
                    "backend_log_suppressed"
                ]
        finally:
            db_store.close_all_connections()
            if previous_db_path is None:
                os.environ.pop("VECTOR_LAKE_DB_PATH", None)
            else:
                os.environ["VECTOR_LAKE_DB_PATH"] = previous_db_path
            if previous_embedding is None:
                os.environ.pop("VECTOR_LAKE_QUERY_EMBEDDING", None)
            else:
                os.environ["VECTOR_LAKE_QUERY_EMBEDDING"] = previous_embedding
        rss_after = _working_set_bytes()
        gc.collect()

        all_results = serial_results + concurrent_results
        errors = [error for _latency, error in all_results if error]
        fallback_errors = [
            error for _latency, error in fallback_results if error
        ]
        report = {
            "contract_version": CONTRACT_VERSION,
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "processor": platform.processor(),
                "workers": workers,
                "remote_query_embeddings": False,
            },
            "corpus": {
                "node_count": node_count,
                "edge_count": max(0, node_count - 1),
                "index_bytes": fixture["index_path"].stat().st_size,
                "database_bytes": fixture["db_path"].stat().st_size,
                "fixture_build_ms": round(fixture["build_ms"], 6),
            },
            "index": {
                "cold_load_ms": round(cold_load_ms, 6),
                "warm_load": _latency_summary(warm_loads),
            },
            "identity_cold_concurrency": identity_concurrency,
            "search": {
                "serial": _latency_summary([item[0] for item in serial_results]),
                "concurrent": _latency_summary(
                    [item[0] for item in concurrent_results]
                ),
                "concurrent_qps": round(
                    len(concurrent_results) / max(concurrent_elapsed, 1e-9), 6
                ),
                "error_count": len(errors),
                "error_rate": round(len(errors) / len(all_results), 6),
                "errors": {
                    name: errors.count(name) for name in sorted(set(errors))
                },
                "last_internal_telemetry": normal_telemetry,
            },
            "fts_fallback": {
                "latency": _latency_summary(
                    [item[0] for item in fallback_results]
                ),
                "error_count": len(fallback_errors),
                "errors": {
                    name: fallback_errors.count(name)
                    for name in sorted(set(fallback_errors))
                },
                "backend_log_suppressed": fallback_suppressed,
                "last_internal_telemetry": fallback_telemetry,
            },
            "memory": {
                "rss_before_mib": (
                    round(rss_before / 1024 / 1024, 6)
                    if rss_before is not None
                    else None
                ),
                "rss_after_mib": (
                    round(rss_after / 1024 / 1024, 6)
                    if rss_after is not None
                    else None
                ),
                "search_rss_delta_mib": (
                    round(max(0, rss_after - rss_before) / 1024 / 1024, 6)
                    if rss_before is not None and rss_after is not None
                    else None
                ),
            },
        }
        report["slo"] = _evaluate_slos(report, effective_slos)
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(REPOSITORY_ROOT / "scratch"),
        help="Existing directory used only as the parent of a temporary fixture.",
    )
    parser.add_argument("--nodes", type=int, default=DEFAULT_NODE_COUNT)
    parser.add_argument("--serial-queries", type=int, default=DEFAULT_SERIAL_QUERIES)
    parser.add_argument(
        "--concurrent-queries", type=int, default=DEFAULT_CONCURRENT_QUERIES
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--fail-on-slo",
        action="store_true",
        help="Return exit code 1 when any SLO fails.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = run_benchmark(
        args.workspace,
        node_count=args.nodes,
        serial_queries=args.serial_queries,
        concurrent_queries=args.concurrent_queries,
        workers=args.workers,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.fail_on_slo and report["slo"]["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
