"""Isolated 10k+ runtime gate for retrieval, timeline, and local-first ingest.

This benchmark creates physical Markdown pages, matching canonical entities, a
projection-v2 search snapshot, FTS rows, and timeline rows under a temporary
MEMORY root.  It never touches the configured production lake and never calls
a remote model.  The ingest gate ends when the deterministic Source mutation
is projected and its linked outbox is index-visible; asynchronous enrichment
is reported separately and is not claimed as measured.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
import os
import platform
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.corpus_scale_benchmark import (  # noqa: E402
    _build_fixture,
    _latency_summary,
    _workload,
)
from vector_lake import (  # noqa: E402
    auto_ingest_worker,
    db_store,
    index_snapshot,
    runtime_health,
    tool_ingest,
    tool_query,
    tool_search,
    tool_timeline,
    watchdog_app,
)

CONTRACT_VERSION = "vector-lake-runtime-10k-gate/v1"
DEFAULT_SLOS = {
    "minimum_wiki_pages": 10_000,
    "retrieval_max_ms": 2_500.0,
    "timeline_max_ms": 2_500.0,
    "local_ingest_index_visible_max_ms": 30_000.0,
    "maximum_error_count": 0,
}
_RETRIEVAL_ERROR_MARKERS = (
    "[Search degraded:",
    "No matching evidence found.",
    "Error executing timeline query:",
    "Context assembly unavailable (",
    "Lake is drying.",
)


class SemanticBenchmarkError(RuntimeError):
    """Raised when a timed call returns a semantically invalid payload."""


_ENV_KEYS = (
    "VECTOR_LAKE_DB_PATH",
    "VECTOR_LAKE_MEMORY_DIR",
    "VECTOR_LAKE_META_DIR",
    "VECTOR_LAKE_QUERY_EMBEDDING",
    "VECTOR_LAKE_MANUAL_QUERY_SYNTHESIS",
    "VECTOR_LAKE_MCP_SURFACE",
)


def _write_auto_ingest_config(memory_dir: Path) -> None:
    meta = memory_dir / "wiki" / ".meta"
    meta.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "enabled": True,
        "allow_model_processing_raw_text": True,
        "runner": "codex_exec",
        "codex_executable": "C:/codex.exe",
        "runner_codex_home": "C:/vector-lake-runtime-benchmark",
        "required_codex_version": "0.148.0",
        "required_codex_sha256": "a" * 64,
        "required_system_skills_sha256": "c" * 64,
        "required_models_cache_sha256": "d" * 64,
        "required_auth_identity_sha256": "b" * 64,
        "model": "benchmark-stub",
        "reasoning_effort": "medium",
        "poll_seconds": 5.0,
        "timeout_seconds": 1200,
        "lease_seconds": 1320,
        "lease_renew_seconds": 120,
        "max_input_bytes": 524288,
        "max_output_bytes": 1048576,
        "max_files": 8,
        "max_attempts_per_revision": 3,
        "max_tasks_per_hour": 100,
        "max_tasks_per_24h": 100,
        "max_tokens_per_task": 32768,
        "max_reserved_tokens_per_hour": 1000000,
        "max_reserved_tokens_per_24h": 1000000,
        "max_consecutive_infra_failures": 3,
        "circuit_breaker_seconds": 3600,
        "max_scratch_runs": 100,
        "scratch_retention_days": 14,
        "retain_artifacts": False,
        "min_decision_confidence": 0.85,
        "auto_finalize_rejected": True,
    }
    (meta / "auto_ingest_config.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )


def _write_purpose_contract(memory_dir: Path) -> None:
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.1"
intent_keywords: [test]
intent_weight_boost: 0.1
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
  derived: Derived operational evidence
sir_registry:
  - id: SIR_BENCHMARK
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Isolated performance benchmark purpose.
""",
        encoding="utf-8",
    )


@contextmanager
def _isolated_runtime(root: Path):
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    db_store.close_all_connections()
    os.environ["VECTOR_LAKE_DB_PATH"] = str(root / "vector_lake_benchmark.db")
    os.environ["VECTOR_LAKE_MEMORY_DIR"] = str(root)
    os.environ["VECTOR_LAKE_META_DIR"] = str(root / "wiki" / ".meta")
    os.environ["VECTOR_LAKE_QUERY_EMBEDDING"] = "0"
    os.environ.pop("VECTOR_LAKE_MANUAL_QUERY_SYNTHESIS", None)
    try:
        yield
    finally:
        db_store.close_all_connections()
        index_snapshot.clear_index_snapshot_cache_for_tests()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _validate_retrieval_output(value, expected: str, surface: str) -> str:
    if not isinstance(value, str):
        raise SemanticBenchmarkError(f"{surface}_result_is_not_text")
    marker = next((item for item in _RETRIEVAL_ERROR_MARKERS if item in value), "")
    if marker:
        marker_offset = value.find(marker)
        detail = value[marker_offset : marker_offset + 120].splitlines()[0]
        raise SemanticBenchmarkError(f"{surface}_returned_{detail}")
    if expected not in value:
        raise SemanticBenchmarkError(f"{surface}_missed_expected_result:{expected}")
    return value


def _measure(callable_, *, cold: bool = False) -> tuple[float, str]:
    if cold:
        db_store.close_connection()
        index_snapshot.clear_index_snapshot_cache_for_tests()
    started = time.perf_counter()
    try:
        callable_()
    except Exception as exc:  # pragma: no cover - exercised by gate failures
        error = type(exc).__name__
        if isinstance(exc, SemanticBenchmarkError):
            error = f"{error}:{str(exc)[:160]}"
        return (time.perf_counter() - started) * 1000.0, error
    return (time.perf_counter() - started) * 1000.0, ""


def _measure_cases(callable_, cases: Sequence[object], *, cold: bool = False) -> dict:
    results = [_measure(lambda case=case: callable_(case), cold=cold) for case in cases]
    errors = [error for _latency, error in results if error]
    return {
        "latency": _latency_summary([latency for latency, _error in results]),
        "error_count": len(errors),
        "errors": {name: errors.count(name) for name in sorted(set(errors))},
    }


def _prewarm_worker(callable_, case, barrier: threading.Barrier) -> float:
    started = time.perf_counter()
    try:
        callable_(case)
    except Exception:
        barrier.abort()
        raise
    barrier.wait(timeout=30)
    return (time.perf_counter() - started) * 1000.0


def _measure_concurrent(callable_, cases: Sequence[object], workers: int) -> dict:
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        barrier = threading.Barrier(workers)
        prewarm_futures = [
            executor.submit(
                _prewarm_worker,
                callable_,
                cases[index % len(cases)],
                barrier,
            )
            for index in range(workers)
        ]
        prewarm_ms = [future.result() for future in prewarm_futures]
        started = time.perf_counter()
        futures = [
            executor.submit(_measure, lambda case=case: callable_(case))
            for case in cases
        ]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started
    errors = [error for _latency, error in results if error]
    return {
        "latency": _latency_summary([latency for latency, _error in results]),
        "qps": round(len(results) / max(elapsed, 1e-9), 6),
        "error_count": len(errors),
        "errors": {name: errors.count(name) for name in sorted(set(errors))},
        "worker_connections_prewarmed": True,
        "worker_prewarm": _latency_summary(prewarm_ms),
    }


def _start_write_interference() -> tuple[
    threading.Event,
    threading.Thread,
    list[str],
    list[int],
    threading.Event,
]:
    stop = threading.Event()
    first_commit = threading.Event()
    errors: list[str] = []
    successful_writes = [0]

    def writer() -> None:
        counter = 0
        connection = sqlite3.connect(str(db_store.get_db_path()), timeout=30.0)
        try:
            while not stop.is_set():
                try:
                    with connection:
                        connection.execute(
                            "UPDATE benchmark_write_probe SET value = ? WHERE id = 1",
                            (counter,),
                        )
                    counter += 1
                    successful_writes[0] = counter
                    first_commit.set()
                except Exception as exc:  # pragma: no cover - gate evidence
                    errors.append(type(exc).__name__)
                    stop.set()
                stop.wait(0.005)
        finally:
            connection.close()

    thread = threading.Thread(target=writer, name="runtime-10k-writer", daemon=True)
    thread.start()
    return stop, thread, errors, successful_writes, first_commit


def _resolve_prepared_job(prepared: dict) -> tuple[str, list[int]]:
    identity_key = db_store._job_idempotency_key("ingest", prepared)
    connection = db_store.get_connection()
    job_row = connection.execute(
        "SELECT job_id FROM jobs WHERE idempotency_key = ?",
        (identity_key,),
    ).fetchone()
    if job_row is None:
        raise RuntimeError("prepared ingest job could not be resolved")
    job_id = str(job_row["job_id"])
    outbox_rows = connection.execute(
        "SELECT outbox_id FROM ingest_outbox_links WHERE job_id = ? ORDER BY outbox_id",
        (job_id,),
    ).fetchall()
    outbox_ids = [int(row["outbox_id"]) for row in outbox_rows]
    if not outbox_ids:
        raise RuntimeError("local Source publication produced no linked outbox")
    return job_id, outbox_ids


def _ingest_one(raw_path: Path) -> tuple[float, str, dict]:
    started = time.perf_counter()
    try:
        prepared = json.loads(
            tool_ingest.prepare_ingest_batch(
                batch_size=1,
                candidate_paths=[str(raw_path)],
            )
        )
        if not isinstance(prepared, dict):
            raise RuntimeError(f"unexpected prepare result: {prepared!r}")
        job_id, outbox_ids = _resolve_prepared_job(prepared)
        connection = db_store.get_connection()
        stats = watchdog_app.process_mutation_outbox_batch(
            limit=max(1, len(outbox_ids)),
            outbox_ids=outbox_ids,
        )
        pending = connection.execute(
            "SELECT COUNT(*) FROM ingest_outbox_links AS link "
            "JOIN mutation_outbox AS outbox ON outbox.id = link.outbox_id "
            "WHERE link.job_id = ? AND outbox.status <> 'completed'",
            (job_id,),
        ).fetchone()[0]
        visible = connection.execute(
            "SELECT COUNT(*) FROM ingest_stage_events "
            "WHERE job_id = ? AND stage = 'index_visible' AND transition = 'completed'",
            (job_id,),
        ).fetchone()[0]
        if pending or not visible or stats["failed"] or stats["retrying"]:
            raise RuntimeError(
                f"Source projection not visible: pending={pending}, visible={visible}, stats={stats}"
            )
        return (
            (time.perf_counter() - started) * 1000.0,
            "",
            {
                "job_id": job_id,
                "outbox_ids": outbox_ids,
                "outbox_stats": stats,
            },
        )
    except Exception as exc:  # pragma: no cover - gate evidence
        return (
            (time.perf_counter() - started) * 1000.0,
            type(exc).__name__,
            {"message": str(exc)[:500]},
        )


def _evaluate(report: dict, slos: dict) -> dict:
    error_count = (
        sum(
            report[section][mode]["error_count"]
            for section, modes in (
                ("search", ("cold", "warm", "concurrent", "write_interference")),
                ("query", ("cold", "warm", "concurrent")),
                ("timeline", ("cold", "warm", "concurrent")),
            )
            for mode in modes
        )
        + report["ingest"]["error_count"]
        + report["ingest"]["asynchronous_enrichment"]["stubbed_orchestration"][
            "error_count"
        ]
    )
    error_count += int(
        bool(report["projection_drift"]["after_convergence"]["search"]["error"])
    )
    error_count += int(
        bool(report["projection_drift"]["after_convergence"]["query"]["error"])
    )
    error_count += len(report["search"]["write_interference"]["writer_errors"])
    checks = {
        "minimum_wiki_pages": report["fixture"]["wiki_file_count"]
        >= int(slos["minimum_wiki_pages"]),
        "search_cold_max": report["search"]["cold"]["latency"]["max_ms"]
        <= float(slos["retrieval_max_ms"]),
        "search_warm_max": report["search"]["warm"]["latency"]["max_ms"]
        <= float(slos["retrieval_max_ms"]),
        "search_concurrent_max": report["search"]["concurrent"]["latency"]["max_ms"]
        <= float(slos["retrieval_max_ms"]),
        "search_write_interference_max": report["search"]["write_interference"][
            "latency"
        ]["max_ms"]
        <= float(slos["retrieval_max_ms"]),
        "write_interference_established": (
            report["search"]["write_interference"]["first_commit_ready"]
            and report["search"]["write_interference"]["successful_write_count"] > 0
            and report["search"]["write_interference"]["overlapping_write_count"] > 0
            and not report["search"]["write_interference"]["writer_errors"]
        ),
        "query_cold_max": report["query"]["cold"]["latency"]["max_ms"]
        <= float(slos["retrieval_max_ms"]),
        "query_warm_max": report["query"]["warm"]["latency"]["max_ms"]
        <= float(slos["retrieval_max_ms"]),
        "query_concurrent_max": report["query"]["concurrent"]["latency"]["max_ms"]
        <= float(slos["retrieval_max_ms"]),
        "timeline_cold_max": report["timeline"]["cold"]["latency"]["max_ms"]
        <= float(slos["timeline_max_ms"]),
        "timeline_warm_max": report["timeline"]["warm"]["latency"]["max_ms"]
        <= float(slos["timeline_max_ms"]),
        "timeline_concurrent_max": report["timeline"]["concurrent"]["latency"]["max_ms"]
        <= float(slos["timeline_max_ms"]),
        "projection_drift_fail_closed": {
            report["projection_drift"]["during_drift"]["search"]["error"],
            report["projection_drift"]["during_drift"]["query"]["error"],
        }
        == {"SearchIndexError"},
        "projection_drift_bounded": max(
            report["projection_drift"]["during_drift"]["search"]["latency_ms"],
            report["projection_drift"]["during_drift"]["query"]["latency_ms"],
        )
        <= float(slos["retrieval_max_ms"]),
        "projection_drift_converged": (
            report["projection_drift"]["pending_after_convergence"] == 0
            and not report["projection_drift"]["after_convergence"]["search"]["error"]
            and not report["projection_drift"]["after_convergence"]["query"]["error"]
        ),
        "projection_converged_latency_max": max(
            report["projection_drift"]["after_convergence"]["search"]["latency_ms"],
            report["projection_drift"]["after_convergence"]["query"]["latency_ms"],
        )
        <= float(slos["retrieval_max_ms"]),
        "local_ingest_index_visible_max": report["ingest"]["latency"]["max_ms"]
        <= float(slos["local_ingest_index_visible_max_ms"]),
        "stubbed_enrichment_orchestration_max": report["ingest"][
            "asynchronous_enrichment"
        ]["stubbed_orchestration"]["latency"]["max_ms"]
        <= float(slos["local_ingest_index_visible_max_ms"]),
        "maximum_error_count": error_count <= int(slos["maximum_error_count"]),
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failures": sorted(name for name, passed in checks.items() if not passed),
        "thresholds": dict(slos),
        "observed_error_count": error_count,
    }


def run_gate(
    workspace: str | os.PathLike,
    *,
    node_count: int = 12_000,
    cold_samples: int = 12,
    warm_samples: int = 80,
    concurrent_samples: int = 160,
    ingest_samples: int = 5,
    workers: int = 8,
    slos: dict | None = None,
) -> dict:
    if (
        node_count < 1
        or min(cold_samples, warm_samples, concurrent_samples, ingest_samples) < 1
    ):
        raise ValueError("all sample counts and node_count must be positive")
    if not 1 <= workers <= 64:
        raise ValueError("workers must be between 1 and 64")
    workspace_path = Path(workspace).expanduser().resolve(strict=True)
    if not workspace_path.is_dir():
        raise ValueError("workspace must be a directory")
    effective_slos = {**DEFAULT_SLOS, **(slos or {})}

    with tempfile.TemporaryDirectory(
        prefix="vector-lake-runtime-10k-", dir=workspace_path
    ) as temporary:
        root = Path(temporary)
        (root / "raw").mkdir(parents=True, exist_ok=True)
        _write_purpose_contract(root)
        _write_auto_ingest_config(root)
        with _isolated_runtime(root):
            fixture = _build_fixture(
                root,
                node_count,
                materialize_wiki=True,
                canonical_entities=True,
                timeline_count=node_count,
            )
            db_store.close_all_connections()
            write_probe_connection = sqlite3.connect(str(fixture["db_path"]))
            try:
                with write_probe_connection:
                    write_probe_connection.execute(
                        "CREATE TABLE benchmark_write_probe "
                        "(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)"
                    )
                    write_probe_connection.execute(
                        "INSERT INTO benchmark_write_probe (id, value) VALUES (1, 0)"
                    )
            finally:
                write_probe_connection.close()
            os.environ["VECTOR_LAKE_MCP_SURFACE"] = "readonly"
            requested_cases = max(warm_samples, concurrent_samples)
            search_cases = [
                (query, expected)
                for query, expected in _workload(node_count, requested_cases * 2)
                if expected is not None
            ][:requested_cases]
            if len(search_cases) != requested_cases:
                raise RuntimeError("could not build enough positive retrieval cases")
            timeline_cases = [
                (
                    f"Synthetic Document {index % node_count:05d}",
                    f"Synthetic Document {index % node_count:05d}",
                )
                for index in range(max(warm_samples, concurrent_samples))
            ]

            def search_call(case: tuple[str, str]) -> str:
                query, expected = case
                return _validate_retrieval_output(
                    tool_search.search_vector_lake(query, top_k=5),
                    expected,
                    "search",
                )

            def query_call(case: tuple[str, str]) -> str:
                query, expected = case
                return _validate_retrieval_output(
                    tool_query.prepare_query_context(query, dry_run=True),
                    expected,
                    "query",
                )

            def timeline_call(case: tuple[str, str]) -> str:
                entity, expected = case
                return _validate_retrieval_output(
                    tool_timeline.search_timeline_events(
                        entity_name=entity,
                        limit=10,
                    ),
                    expected,
                    "timeline",
                )

            search = {
                "cold": _measure_cases(
                    search_call, search_cases[:cold_samples], cold=True
                ),
                "warm": _measure_cases(search_call, search_cases[:warm_samples]),
                "concurrent": _measure_concurrent(
                    search_call, search_cases[:concurrent_samples], workers
                ),
            }
            db_store.close_all_connections()
            os.environ["VECTOR_LAKE_MCP_SURFACE"] = "full"
            (
                stop,
                writer,
                writer_errors,
                successful_writes,
                first_commit,
            ) = _start_write_interference()
            first_commit_ready = first_commit.wait(timeout=5)
            writes_before_measurement = successful_writes[0]
            try:
                search["write_interference"] = _measure_cases(
                    search_call,
                    search_cases[:warm_samples],
                )
            finally:
                stop.set()
                writer.join(timeout=5)
            writes_after_measurement = successful_writes[0]
            search["write_interference"].update(
                {
                    "first_commit_ready": first_commit_ready,
                    "successful_write_count": writes_after_measurement,
                    "overlapping_write_count": max(
                        0,
                        writes_after_measurement - writes_before_measurement,
                    ),
                    "writer_errors": list(writer_errors),
                }
            )
            db_store.close_all_connections()
            os.environ["VECTOR_LAKE_MCP_SURFACE"] = "readonly"

            query = {
                "cold": _measure_cases(
                    query_call, search_cases[:cold_samples], cold=True
                ),
                "warm": _measure_cases(query_call, search_cases[:warm_samples]),
                "concurrent": _measure_concurrent(
                    query_call, search_cases[:concurrent_samples], workers
                ),
            }
            timeline = {
                "cold": _measure_cases(
                    timeline_call, timeline_cases[:cold_samples], cold=True
                ),
                "warm": _measure_cases(timeline_call, timeline_cases[:warm_samples]),
                "concurrent": _measure_concurrent(
                    timeline_call, timeline_cases[:concurrent_samples], workers
                ),
            }

            db_store.close_all_connections()
            os.environ["VECTOR_LAKE_MCP_SURFACE"] = "full"
            ingest_results = []
            with patch.object(
                runtime_health,
                "enforce_runtime_write_health",
                lambda validation_mode="full": {
                    "status": "ok",
                    "mode": validation_mode,
                },
            ):
                drift_raw = root / "raw" / "benchmark-projection-drift.md"
                drift_raw.write_text(
                    "Deterministic projection drift benchmark with test evidence.",
                    encoding="utf-8",
                )
                drift_prepared = json.loads(
                    tool_ingest.prepare_ingest_batch(
                        batch_size=1,
                        candidate_paths=[str(drift_raw)],
                    )
                )
                drift_job_id, drift_outbox_ids = _resolve_prepared_job(drift_prepared)
                drift_connection = db_store.get_connection()
                pending_before = int(
                    drift_connection.execute(
                        "SELECT COUNT(*) FROM ingest_outbox_links AS link "
                        "JOIN mutation_outbox AS outbox ON outbox.id = link.outbox_id "
                        "WHERE link.job_id = ? AND outbox.status <> 'completed'",
                        (drift_job_id,),
                    ).fetchone()[0]
                )
                drift_case = ("needle00000", "Synthetic Document 00000")
                drift_search_ms, drift_search_error = _measure(
                    lambda: search_call(drift_case)
                )
                drift_query_ms, drift_query_error = _measure(
                    lambda: query_call(drift_case)
                )
                convergence_stats = watchdog_app.process_mutation_outbox_batch(
                    limit=max(1, len(drift_outbox_ids)),
                    outbox_ids=drift_outbox_ids,
                )
                pending_after = int(
                    drift_connection.execute(
                        "SELECT COUNT(*) FROM ingest_outbox_links AS link "
                        "JOIN mutation_outbox AS outbox ON outbox.id = link.outbox_id "
                        "WHERE link.job_id = ? AND outbox.status <> 'completed'",
                        (drift_job_id,),
                    ).fetchone()[0]
                )
                converged_search_ms, converged_search_error = _measure(
                    lambda: search_call(drift_case)
                )
                converged_query_ms, converged_query_error = _measure(
                    lambda: query_call(drift_case)
                )
                projection_drift = {
                    "job_id": drift_job_id,
                    "outbox_ids": drift_outbox_ids,
                    "pending_before_convergence": pending_before,
                    "pending_after_convergence": pending_after,
                    "during_drift": {
                        "search": {
                            "latency_ms": round(drift_search_ms, 6),
                            "error": drift_search_error,
                        },
                        "query": {
                            "latency_ms": round(drift_query_ms, 6),
                            "error": drift_query_error,
                        },
                    },
                    "after_convergence": {
                        "search": {
                            "latency_ms": round(converged_search_ms, 6),
                            "error": converged_search_error,
                        },
                        "query": {
                            "latency_ms": round(converged_query_ms, 6),
                            "error": converged_query_error,
                        },
                    },
                    "convergence_stats": convergence_stats,
                }

                for index in range(ingest_samples):
                    raw_path = root / "raw" / f"benchmark-ingest-{index:03d}.md"
                    raw_path.write_text(
                        f"Deterministic benchmark source {index:03d} with test evidence.",
                        encoding="utf-8",
                    )
                    ingest_results.append(_ingest_one(raw_path))

                ingest_job_ids = [
                    str(details["job_id"])
                    for _latency, error, details in ingest_results
                    if not error and details.get("job_id")
                ]
                with db_store.transaction():
                    db_store.get_connection().executemany(
                        "UPDATE jobs SET status = 'awaiting_subagent' WHERE job_id = ?",
                        [(job_id,) for job_id in ingest_job_ids],
                    )

                def fake_generator(
                    _executable,
                    _config,
                    job_id,
                    _lease,
                    _prompt,
                    _stop_event,
                    _health_check=None,
                ):
                    return {
                        "schema_version": 1,
                        "job_id": job_id,
                        "purpose_scope": "excluded",
                        "purpose_evidence": "Benchmark stub excludes enrichment content.",
                        "decision_confidence": 0.99,
                        "files": [],
                        "integration": {
                            "disposition": "rejected",
                            "reason": "Benchmark measures orchestration without a remote model.",
                            "relations": [],
                        },
                    }

                controller = auto_ingest_worker.AutoIngestController()
                enrichment_outcomes: list[str] = []

                def finalize_stubbed_enrichment() -> None:
                    result = controller.tick(threading.Event())
                    enrichment_outcomes.append(result)
                    if result != "finalized":
                        raise RuntimeError(f"unexpected enrichment result: {result}")

                enrichment_results = []
                with (
                    patch.object(
                        auto_ingest_worker,
                        "_claimable_job_exists",
                        lambda: True,
                    ),
                    patch.object(
                        auto_ingest_worker,
                        "_probe_codex_runner",
                        lambda _config: Path("C:/codex.exe"),
                    ),
                    patch.object(
                        auto_ingest_worker,
                        "_run_codex_generator",
                        fake_generator,
                    ),
                ):
                    for _job_id in ingest_job_ids:
                        enrichment_results.append(_measure(finalize_stubbed_enrichment))
            ingest_errors = [
                error for _latency, error, _details in ingest_results if error
            ]
            enrichment_errors = [
                error for _latency, error in enrichment_results if error
            ]
            ingest = {
                "scope": "raw revision to deterministic Source index_visible",
                "runtime_health_gate_stubbed": True,
                "latency": _latency_summary(
                    [latency for latency, _error, _details in ingest_results]
                ),
                "error_count": len(ingest_errors),
                "errors": {
                    name: ingest_errors.count(name)
                    for name in sorted(set(ingest_errors))
                },
                "samples": [details for _latency, _error, details in ingest_results],
                "asynchronous_enrichment": {
                    "remote_model": {
                        "status": "not_measured",
                        "reason": "No remote provider is called by this isolated benchmark.",
                    },
                    "stubbed_orchestration": {
                        "status": "measured",
                        "latency": _latency_summary(
                            [latency for latency, _error in enrichment_results]
                        ),
                        "error_count": len(enrichment_errors),
                        "errors": {
                            name: enrichment_errors.count(name)
                            for name in sorted(set(enrichment_errors))
                        },
                        "outcomes": enrichment_outcomes,
                    },
                },
            }

            report = {
                "contract_version": CONTRACT_VERSION,
                "runtime": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "workers": workers,
                    "remote_query_embeddings": False,
                    "isolated_memory_root": True,
                },
                "fixture": {
                    "requested_node_count": node_count,
                    "wiki_file_count": fixture["wiki_file_count"],
                    "canonical_entity_count": fixture["canonical_entity_count"],
                    "timeline_count": fixture["timeline_count"],
                    "build_ms": round(fixture["build_ms"], 6),
                    "database_bytes": fixture["db_path"].stat().st_size,
                },
                "search": search,
                "query": query,
                "timeline": timeline,
                "projection_drift": projection_drift,
                "ingest": ingest,
                "cold_definition": "in-process projection cache cleared and thread-local SQLite connection closed; OS cache not cleared",
                "concurrent_definition": (
                    "executor worker connections and integrity proofs are prewarmed "
                    "before timed calls; executor queue delay is excluded"
                ),
            }
            report["slo"] = _evaluate(report, effective_slos)
            return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--nodes", type=int, default=12_000)
    parser.add_argument("--cold-samples", type=int, default=12)
    parser.add_argument("--warm-samples", type=int, default=80)
    parser.add_argument("--concurrent-samples", type=int, default=160)
    parser.add_argument("--ingest-samples", type=int, default=5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--fail-on-slo", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = run_gate(
        args.workspace,
        node_count=args.nodes,
        cold_samples=args.cold_samples,
        warm_samples=args.warm_samples,
        concurrent_samples=args.concurrent_samples,
        ingest_samples=args.ingest_samples,
        workers=args.workers,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if args.fail_on_slo and report["slo"]["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
