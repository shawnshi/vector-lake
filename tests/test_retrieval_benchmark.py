import hashlib
import json

import pytest

from vector_lake.retrieval_benchmark import (
    BENCHMARK_CONTRACT,
    RetrievalBenchmarkError,
    evaluate_rankings,
    run_retrieval_benchmark,
)


def _dataset() -> dict:
    return {
        "contract_version": BENCHMARK_CONTRACT,
        "dataset_id": "unit-retrieval",
        "dataset_version": "1",
        "top_k": 2,
        "thresholds": {"recall_at_k": 0.7},
        "queries": [
            {
                "id": "q1",
                "query": "alpha",
                "relevant_keys": ["Concept_A", "Concept_B"],
            },
            {
                "id": "q2",
                "query": "gamma",
                "relevant_keys": ["Concept_C"],
            },
        ],
    }


def _xml(*keys: str) -> str:
    nodes = "".join(
        f'<Evidence_Node ID="Wiki_{index}" Source="{key}.md">x</Evidence_Node>'
        for index, key in enumerate(keys)
    )
    readiness = json.dumps({
        "contract_version": "vector-lake-semantic-readiness-envelope/v1",
        "status": "not_ready",
        "results_are_not_accepted_facts": True,
    })
    return (
        f'<VectorLakeSearchResponse><SemanticReadinessEnvelope>{readiness}'
        '</SemanticReadinessEnvelope><EvidenceResults>'
        f'<SearchStatus State="ok" Backends=""/>{nodes}'
        '</EvidenceResults></VectorLakeSearchResponse>'
    )


def test_evaluate_rankings_is_deterministic():
    rankings = {"q1": ["Concept_A", "Concept_X"], "q2": ["Concept_X", "Concept_C"]}

    first = evaluate_rankings(_dataset(), rankings)
    second = evaluate_rankings(_dataset(), rankings)

    assert first == second
    assert first["status"] == "pass"
    assert first["metrics"]["precision_at_k"] == 0.5
    assert first["metrics"]["recall_at_k"] == 0.75
    assert first["metrics"]["mrr"] == 0.75


def test_run_benchmark_binds_dataset_hash_and_disables_remote_embeddings(
    tmp_path,
    monkeypatch,
):
    dataset_bytes = json.dumps(_dataset(), sort_keys=True).encode("utf-8")
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_bytes(dataset_bytes)
    monkeypatch.setenv("VECTOR_LAKE_QUERY_EMBEDDING", "1")
    observed = []

    def search(query, **kwargs):
        observed.append((query, kwargs, __import__("os").environ["VECTOR_LAKE_QUERY_EMBEDDING"]))
        return _xml("Concept_A", "Concept_X") if query == "alpha" else _xml("Concept_X", "Concept_C")

    report = run_retrieval_benchmark(dataset_path, search_fn=search)

    assert report["dataset_sha256"] == hashlib.sha256(dataset_bytes).hexdigest()
    assert report["retrieval_config"]["remote_query_embeddings"] is False
    assert all(item[2] == "0" for item in observed)
    assert __import__("os").environ["VECTOR_LAKE_QUERY_EMBEDDING"] == "1"
    assert all(item[1]["as_xml"] is True for item in observed)


@pytest.mark.parametrize(("keys", "expected_exit", "expected_status"), [
    (("Concept_A", "Concept_B", "Concept_C"), 0, "pass"),
    (("Concept_Miss",), 2, "fail"),
])
def test_cli_retrieval_threshold_status_controls_exit_code(
    tmp_path, monkeypatch, capsys, keys, expected_exit, expected_status,
):
    from vector_lake import cli_app, retrieval_benchmark

    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    monkeypatch.setattr(cli_app, "_configure_stdout", lambda: None)
    report = run_retrieval_benchmark(
        dataset_path,
        search_fn=lambda query, **_kwargs: (
            _xml("Concept_C") if query == "gamma" and expected_exit == 0 else _xml(*keys)
        ),
    )
    monkeypatch.setattr(retrieval_benchmark, "run_retrieval_benchmark", lambda *_a, **_kw: report)
    monkeypatch.setattr(cli_app, "_cli_heavy_task_policy", lambda _args: None)
    monkeypatch.setattr("sys.argv", ["cli.py", "retrieval-benchmark", str(dataset_path)])

    assert cli_app.main() == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == expected_status


def test_cli_retrieval_invalid_input_is_execution_error(tmp_path, monkeypatch, capsys):
    from vector_lake import cli_app

    monkeypatch.setattr(cli_app, "_configure_stdout", lambda: None)
    dataset_path = tmp_path / "invalid.json"
    dataset_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_app, "_cli_heavy_task_policy", lambda _args: None)
    monkeypatch.setattr("sys.argv", ["cli.py", "retrieval-benchmark", str(dataset_path)])

    assert cli_app.main() == 1
    assert "Error executing command" in capsys.readouterr().err


@pytest.mark.parametrize("payload", [
    "not XML",
    "<EvidenceResults/>",
    "<VectorLakeSearchResponse/>",
    _xml().replace("EvidenceResults", "MemoryResults"),
    _xml().replace('State="ok"', 'State="unavailable"'),
    _xml().replace('State="ok"', 'State="error"'),
    _xml().replace("</VectorLakeSearchResponse>", "<EvidenceResults/></VectorLakeSearchResponse>"),
    _xml().replace("</EvidenceResults>", "<Error>failed</Error></EvidenceResults>"),
    _xml().replace("</EvidenceResults>", "<Evidence_Node/></EvidenceResults>"),
    _xml().replace('"not_ready"', 'invalid-json'),
    _xml().replace('"not_ready"', '"unavailable"'),
    _xml().replace('<SearchStatus State="ok" Backends=""/>', '<SearchStatus State="ok" Backends="">failed</SearchStatus>'),
    _xml().replace('</EvidenceResults>', '<NoEvidence><Error/></NoEvidence></EvidenceResults>'),
    _xml("Concept_A").replace('>x</Evidence_Node>', '><Error/></Evidence_Node>'),
    _xml("Concept_A").replace("</EvidenceResults>", "<NoEvidence/></EvidenceResults>"),
])
def test_benchmark_rejects_unavailable_malformed_and_wrong_mode(tmp_path, payload):
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    with pytest.raises(RetrievalBenchmarkError):
        run_retrieval_benchmark(dataset_path, search_fn=lambda *_a, **_k: payload)


def test_benchmark_preserves_advisory_and_degradation_metadata(tmp_path):
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    report = run_retrieval_benchmark(
        dataset_path,
        search_fn=lambda *_a, **_k: _xml("Concept_A", "Concept_B", "Concept_C").replace(
            'State="ok" Backends=""', 'State="degraded" Backends="fts5"'
        ),
    )
    assert report["queries"][0]["search_status"] == {"State": "degraded", "Backends": "fts5"}
    assert report["queries"][0]["semantic_readiness"]["status"] == "not_ready"


def test_benchmark_uses_real_search_envelope_and_healthy_zero_hits(isolated_memory):
    from vector_lake import db_store, governance_store, indexer, tool_search

    db_store.init_db()
    governance_store.upsert_entity("entity_benchmark", {
        "entity_id": "entity_benchmark", "page_key": "Concept_Benchmark",
        "canonical_name": "Benchmark corpus", "type": "concept",
        "status": "active", "raw_text": "Benchmark corpus evidence.",
    })
    (isolated_memory / "wiki" / "Concept_Benchmark.md").write_text(
        "---\ntitle: Benchmark corpus\n---\nBenchmark corpus evidence.\n", encoding="utf-8"
    )
    indexer.generate_index()
    dataset = _dataset()
    dataset["queries"] = [
        {"id": "hit", "query": "Concept_Benchmark", "relevant_keys": ["Concept_Benchmark"]},
        {"id": "miss", "query": "absentzzzzzz", "relevant_keys": ["Concept_Benchmark"]},
    ]
    dataset_path = isolated_memory / "dataset.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    report = run_retrieval_benchmark(dataset_path)
    hit, miss = report["queries"]
    assert hit["retrieved_keys"] == ["Concept_Benchmark"]
    assert miss["retrieved_keys"] == []
    assert miss["metrics"]["recall_at_k"] == 0
    assert hit["search_status"]["State"] == miss["search_status"]["State"] == "ok"
    assert hit["semantic_readiness"]["results_are_not_accepted_facts"] is True
    assert "VectorLakeSearchResponse" in tool_search.search_vector_lake("Concept_Benchmark", as_xml=True)


def test_benchmark_missing_real_index_is_execution_error(isolated_memory):
    dataset_path = isolated_memory / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    with pytest.raises(RetrievalBenchmarkError):
        run_retrieval_benchmark(dataset_path)
    assert not (isolated_memory / "wiki" / "index.json").exists()


def test_benchmark_rejects_duplicate_query_ids():
    dataset = _dataset()
    dataset["queries"][1]["id"] = "q1"

    with pytest.raises(RetrievalBenchmarkError, match="duplicate query id"):
        evaluate_rankings(dataset, {})
