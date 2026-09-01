import pytest

from benchmarks.runtime_10k_gate import (
    SemanticBenchmarkError,
    _measure_cases,
    _validate_retrieval_output,
    run_gate,
)
from vector_lake import tool_search


def test_fts_signature_ignores_revalidated_connection_revision_noise():
    proof = ("generation", "canonical", 10, "digest", "updated")
    assert tool_search._stable_fts_projection_signature((*proof, 1, 2, 3)) == (
        tool_search._stable_fts_projection_signature((*proof, 4, 5, 6))
    )
    assert tool_search._stable_fts_projection_signature(
        ("new-generation", *proof[1:], 4, 5, 6)
    ) != tool_search._stable_fts_projection_signature((*proof, 1, 2, 3))


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        ("Error executing timeline query: database unavailable", "Expected"),
        ("No matching evidence found.", "Expected"),
        ("A valid-looking but unrelated result", "Expected"),
        ("[Search degraded: fts5]\nExpected", "Expected"),
    ],
)
def test_semantic_retrieval_failures_are_counted(returned, expected):
    measured = _measure_cases(
        lambda case: _validate_retrieval_output(returned, case[1], "test"),
        [("query", expected)],
    )

    assert measured["error_count"] == 1
    assert len(measured["errors"]) == 1
    assert next(iter(measured["errors"])).startswith("SemanticBenchmarkError:")


def test_valid_retrieval_output_is_accepted():
    assert _validate_retrieval_output("prefix Expected suffix", "Expected", "test")
    with pytest.raises(SemanticBenchmarkError):
        _validate_retrieval_output(None, "Expected", "test")


def test_small_runtime_gate_exercises_all_surfaces(tmp_path):
    report = run_gate(
        tmp_path,
        node_count=32,
        cold_samples=2,
        warm_samples=3,
        concurrent_samples=4,
        ingest_samples=1,
        workers=2,
        slos={"minimum_wiki_pages": 32},
    )

    assert report["fixture"]["wiki_file_count"] == 32
    assert report["fixture"]["canonical_entity_count"] == 32
    assert report["fixture"]["timeline_count"] == 32
    assert report["search"]["concurrent"]["latency"]["samples"] == 4
    assert report["search"]["concurrent"]["worker_connections_prewarmed"] is True
    assert report["search"]["concurrent"]["worker_prewarm"]["samples"] == 2
    assert report["search"]["write_interference"]["first_commit_ready"] is True
    assert report["search"]["write_interference"]["successful_write_count"] > 0
    assert report["search"]["write_interference"]["overlapping_write_count"] > 0
    assert report["search"]["write_interference"]["writer_errors"] == []
    assert report["query"]["cold"]["latency"]["samples"] == 2
    assert report["timeline"]["warm"]["latency"]["samples"] == 3
    for section, modes in {
        "search": ("cold", "warm", "concurrent", "write_interference"),
        "query": ("cold", "warm", "concurrent"),
        "timeline": ("cold", "warm", "concurrent"),
    }.items():
        for mode in modes:
            assert report[section][mode]["error_count"] == 0, report[section][mode]
    assert report["projection_drift"]["pending_before_convergence"] == 1
    assert report["projection_drift"]["during_drift"]["search"]["error"] == (
        "SearchIndexError"
    )
    assert report["projection_drift"]["during_drift"]["query"]["error"] == (
        "SearchIndexError"
    )
    assert report["projection_drift"]["pending_after_convergence"] == 0
    assert report["projection_drift"]["after_convergence"]["search"]["error"] == ""
    assert report["ingest"]["latency"]["samples"] == 1
    assert report["ingest"]["asynchronous_enrichment"]["remote_model"]["status"] == (
        "not_measured"
    )
    stubbed = report["ingest"]["asynchronous_enrichment"]["stubbed_orchestration"]
    assert stubbed["latency"]["samples"] == 1
    assert stubbed["error_count"] == 0, stubbed
    assert report["slo"]["status"] == "pass", report["slo"]
