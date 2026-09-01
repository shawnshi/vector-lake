import importlib.util
from pathlib import Path


_MODULE_PATH = Path(__file__).parents[1] / "benchmarks" / "corpus_scale_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("corpus_scale_benchmark", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)


def test_percentile_interpolates_deterministically():
    assert benchmark._percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert benchmark._percentile([5.0], 0.95) == 5.0
    assert benchmark._percentile([], 0.95) == 0.0


def test_small_fixture_exercises_search_and_reports_minimum_corpus_failure(
    tmp_path,
    monkeypatch,
):
    production_sentinel = tmp_path / "production-sentinel"
    production_meta = production_sentinel / "wiki" / ".meta"
    production_meta.mkdir(parents=True)
    marker = production_sentinel / "must-not-be-touched.txt"
    marker.write_text("sentinel", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(production_sentinel))
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", str(production_meta))

    report = benchmark.run_benchmark(
        workspace,
        node_count=256,
        serial_queries=15,
        concurrent_queries=24,
        workers=4,
    )

    assert report["contract_version"] == benchmark.CONTRACT_VERSION
    assert report["corpus"]["node_count"] == 256
    assert report["search"]["error_count"] == 0
    assert report["search"]["worker_connections_prewarmed"] is True
    assert report["search"]["worker_prewarm"]["samples"] == 4
    assert report["search"]["negative"]["samples"] == 20
    assert report["search"]["negative_error_count"] == 0
    assert report["fts_fallback"]["error_count"] == 0
    assert report["fts_fallback"]["backend_log_suppressed"] == {"fts5": 19}
    assert report["runtime"]["remote_query_embeddings"] is False
    assert report["runtime"]["semantic_readiness_stubbed"] is True
    assert marker.read_text(encoding="utf-8") == "sentinel"
    assert [path for path in production_sentinel.rglob("*") if path.is_file()] == [
        marker
    ]
    assert report["slo"]["checks"]["minimum_node_count"] is False
    assert "minimum_node_count" in report["slo"]["failures"]
    assert report["identity_cold_concurrency"]["build_passes"] >= 1


def test_slo_evaluator_rejects_identity_rebuild_fanout():
    report = {
        "corpus": {"node_count": 12_000},
        "index": {
            "cold_load_ms": 100.0,
            "warm_load": {"p95_ms": 1.0},
        },
        "identity_cold_concurrency": {"build_passes": 8},
        "search": {
            "serial": {"p95_ms": 10.0, "max_ms": 20.0},
            "negative": {"p95_ms": 10.0, "max_ms": 20.0},
            "negative_error_count": 0,
            "concurrent": {"p95_ms": 20.0, "max_ms": 20.0},
            "concurrent_qps": 100.0,
            "error_rate": 0.0,
        },
        "fts_fallback": {
            "latency": {"p95_ms": 30.0},
            "error_count": 0,
        },
        "memory": {"search_rss_delta_mib": 20.0},
    }

    result = benchmark._evaluate_slos(report, benchmark.DEFAULT_SLOS)

    assert result["status"] == "fail"
    assert result["failures"] == ["identity_cold_build_passes"]


def test_slo_evaluator_enforces_hard_concurrent_latency_ceiling():
    report = {
        "corpus": {"node_count": 12_000},
        "index": {"cold_load_ms": 100.0, "warm_load": {"p95_ms": 1.0}},
        "identity_cold_concurrency": {"build_passes": 1},
        "search": {
            "serial": {"p95_ms": 10.0, "max_ms": 20.0},
            "negative": {"p95_ms": 10.0, "max_ms": 20.0},
            "negative_error_count": 0,
            "concurrent": {"p95_ms": 20.0, "max_ms": 2_501.0},
            "concurrent_qps": 100.0,
            "error_rate": 0.0,
        },
        "fts_fallback": {"latency": {"p95_ms": 30.0}, "error_count": 0},
        "memory": {"search_rss_delta_mib": 20.0},
    }

    result = benchmark._evaluate_slos(report, benchmark.DEFAULT_SLOS)

    assert result["status"] == "fail"
    assert result["failures"] == ["concurrent_search_max_ms"]
