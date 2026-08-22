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
    return f'<EvidenceResults><SearchStatus State="ok" Backends=""/>{nodes}</EvidenceResults>'


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


def test_benchmark_rejects_duplicate_query_ids():
    dataset = _dataset()
    dataset["queries"][1]["id"] = "q1"

    with pytest.raises(RetrievalBenchmarkError, match="duplicate query id"):
        evaluate_rankings(dataset, {})
