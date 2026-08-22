"""Deterministic, read-only retrieval benchmark runner."""

from __future__ import annotations

import hashlib
import json
import math
import os
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


BENCHMARK_CONTRACT = "vector-lake-retrieval-benchmark/v1"
EVALUATOR_VERSION = "vector-lake-retrieval-evaluator/1.0"
_MAX_DATASET_BYTES = 5 * 1024 * 1024
_MAX_QUERIES = 10_000
_ALLOWED_THRESHOLD_METRICS = frozenset(
    {"precision_at_k", "recall_at_k", "mrr", "ndcg_at_k"}
)


class RetrievalBenchmarkError(ValueError):
    """The benchmark dataset or a retrieval result violates the v1 contract."""


def _positive_top_k(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RetrievalBenchmarkError("top_k must be an integer") from exc
    if not 1 <= parsed <= 100:
        raise RetrievalBenchmarkError("top_k must be between 1 and 100")
    return parsed


def _load_dataset(path: str | os.PathLike) -> tuple[dict, str]:
    dataset_path = Path(path).expanduser().resolve(strict=True)
    if not dataset_path.is_file():
        raise RetrievalBenchmarkError("benchmark dataset must be a regular file")
    if dataset_path.stat().st_size > _MAX_DATASET_BYTES:
        raise RetrievalBenchmarkError(
            f"benchmark dataset exceeds {_MAX_DATASET_BYTES} bytes"
        )
    payload = dataset_path.read_bytes()
    try:
        decoded = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalBenchmarkError(
            "benchmark dataset must be strict UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise RetrievalBenchmarkError("benchmark dataset must be a JSON object")
    return decoded, hashlib.sha256(payload).hexdigest()


def _validate_dataset(dataset: dict) -> dict:
    if dataset.get("contract_version") != BENCHMARK_CONTRACT:
        raise RetrievalBenchmarkError(
            f"unsupported benchmark contract: {dataset.get('contract_version')!r}"
        )
    dataset_id = str(dataset.get("dataset_id") or "").strip()
    dataset_version = str(dataset.get("dataset_version") or "").strip()
    if not dataset_id or not dataset_version:
        raise RetrievalBenchmarkError("dataset_id and dataset_version are required")
    top_k = _positive_top_k(dataset.get("top_k", 5))
    raw_queries = dataset.get("queries")
    if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= _MAX_QUERIES:
        raise RetrievalBenchmarkError(
            f"queries must contain 1..{_MAX_QUERIES} cases"
        )

    queries = []
    seen_ids = set()
    for position, raw_case in enumerate(raw_queries):
        if not isinstance(raw_case, dict):
            raise RetrievalBenchmarkError(f"query case {position} must be an object")
        case_id = str(raw_case.get("id") or "").strip()
        query = str(raw_case.get("query") or "").strip()
        relevant = raw_case.get("relevant_keys")
        if not case_id or not query:
            raise RetrievalBenchmarkError(
                f"query case {position} requires non-empty id and query"
            )
        if case_id in seen_ids:
            raise RetrievalBenchmarkError(f"duplicate query id: {case_id}")
        seen_ids.add(case_id)
        if not isinstance(relevant, list) or not relevant:
            raise RetrievalBenchmarkError(
                f"query case {case_id} requires relevant_keys"
            )
        normalized_relevant = []
        for key in relevant:
            normalized = str(key or "").strip()
            if not normalized:
                raise RetrievalBenchmarkError(
                    f"query case {case_id} contains an empty relevant key"
                )
            if normalized.endswith(".md"):
                normalized = normalized[:-3]
            if normalized not in normalized_relevant:
                normalized_relevant.append(normalized)
        queries.append(
            {
                "id": case_id,
                "query": query,
                "relevant_keys": normalized_relevant,
                "domain": str(raw_case.get("domain") or "").strip() or None,
                "cluster": str(raw_case.get("cluster") or "").strip() or None,
                "include_history": bool(raw_case.get("include_history", False)),
            }
        )

    raw_thresholds = dataset.get("thresholds") or {}
    if not isinstance(raw_thresholds, dict):
        raise RetrievalBenchmarkError("thresholds must be an object")
    unknown = sorted(set(raw_thresholds) - _ALLOWED_THRESHOLD_METRICS)
    if unknown:
        raise RetrievalBenchmarkError(f"unsupported threshold metrics: {unknown}")
    thresholds = {}
    for metric, value in raw_thresholds.items():
        try:
            threshold = float(value)
        except (TypeError, ValueError) as exc:
            raise RetrievalBenchmarkError(
                f"threshold {metric} must be numeric"
            ) from exc
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise RetrievalBenchmarkError(
                f"threshold {metric} must be between 0 and 1"
            )
        thresholds[metric] = threshold

    return {
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "top_k": top_k,
        "queries": queries,
        "thresholds": thresholds,
    }


def _metrics_for_ranking(
    ranked_keys: list[str], relevant_keys: list[str], top_k: int
) -> dict[str, float | int]:
    relevant = set(relevant_keys)
    ranked = ranked_keys[:top_k]
    hit_positions = [
        position
        for position, key in enumerate(ranked, start=1)
        if key in relevant
    ]
    hit_count = len(hit_positions)
    dcg = sum(1.0 / math.log2(position + 1) for position in hit_positions)
    ideal_hits = min(len(relevant), top_k)
    idcg = sum(
        1.0 / math.log2(position + 1)
        for position in range(1, ideal_hits + 1)
    )
    return {
        "hit_count": hit_count,
        "precision_at_k": round(hit_count / top_k, 6),
        "recall_at_k": round(hit_count / len(relevant), 6),
        "mrr": round(1.0 / hit_positions[0], 6) if hit_positions else 0.0,
        "ndcg_at_k": round(dcg / idcg, 6) if idcg else 0.0,
    }


def evaluate_rankings(
    dataset: dict,
    rankings: dict[str, list[str]],
    *,
    top_k_override: int | None = None,
) -> dict:
    """Evaluate pre-ranked keys; useful for deterministic tests and adapters."""
    normalized = _validate_dataset(dataset)
    top_k = (
        _positive_top_k(top_k_override)
        if top_k_override is not None
        else normalized["top_k"]
    )
    per_query = []
    for case in normalized["queries"]:
        raw_ranking = rankings.get(case["id"], [])
        ranking = []
        for key in raw_ranking:
            normalized_key = str(key or "").strip()
            if normalized_key.endswith(".md"):
                normalized_key = normalized_key[:-3]
            if normalized_key and normalized_key not in ranking:
                ranking.append(normalized_key)
        metrics = _metrics_for_ranking(
            ranking, case["relevant_keys"], top_k
        )
        per_query.append(
            {
                "id": case["id"],
                "query": case["query"],
                "relevant_keys": case["relevant_keys"],
                "retrieved_keys": ranking[:top_k],
                "metrics": metrics,
            }
        )

    metric_names = ("precision_at_k", "recall_at_k", "mrr", "ndcg_at_k")
    aggregate = {
        name: round(
            sum(float(item["metrics"][name]) for item in per_query)
            / len(per_query),
            6,
        )
        for name in metric_names
    }
    failures = {
        name: {"actual": aggregate[name], "required": required}
        for name, required in normalized["thresholds"].items()
        if aggregate[name] < required
    }
    return {
        "contract_version": BENCHMARK_CONTRACT,
        "evaluator_version": EVALUATOR_VERSION,
        "dataset_id": normalized["dataset_id"],
        "dataset_version": normalized["dataset_version"],
        "top_k": top_k,
        "sample_count": len(per_query),
        "metrics": aggregate,
        "thresholds": normalized["thresholds"],
        "failures": failures,
        "status": "pass" if not failures else "fail",
        "queries": per_query,
    }


def _ranked_keys_from_xml(payload: str) -> list[str]:
    try:
        root = ET.fromstring(str(payload or ""))
    except ET.ParseError as exc:
        raise RetrievalBenchmarkError(
            "search did not return a parseable EvidenceResults envelope"
        ) from exc
    if root.tag != "EvidenceResults":
        raise RetrievalBenchmarkError(
            f"unexpected search result root: {root.tag}"
        )
    ranked = []
    for node in root.findall("Evidence_Node"):
        source = str(node.attrib.get("Source") or "").strip()
        if source.endswith(".md"):
            source = source[:-3]
        if source and source not in ranked:
            ranked.append(source)
    return ranked


@contextmanager
def _embedding_policy(allow_remote_embeddings: bool) -> Iterator[None]:
    key = "VECTOR_LAKE_QUERY_EMBEDDING"
    previous = os.environ.get(key)
    if not allow_remote_embeddings:
        os.environ[key] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def run_retrieval_benchmark(
    dataset_path: str | os.PathLike,
    *,
    top_k_override: int | None = None,
    allow_remote_embeddings: bool = False,
    search_fn: Callable[..., str] | None = None,
) -> dict:
    """Run a read-only benchmark and bind the report to exact input bytes."""
    dataset, dataset_sha256 = _load_dataset(dataset_path)
    normalized = _validate_dataset(dataset)
    top_k = (
        _positive_top_k(top_k_override)
        if top_k_override is not None
        else normalized["top_k"]
    )
    if search_fn is None:
        from vector_lake.tool_search import search_vector_lake

        search_fn = search_vector_lake

    rankings = {}
    with _embedding_policy(allow_remote_embeddings):
        for case in normalized["queries"]:
            result = search_fn(
                case["query"],
                top_k=top_k,
                as_xml=True,
                domain=case["domain"],
                cluster=case["cluster"],
                include_history=case["include_history"],
                mode="page",
            )
            rankings[case["id"]] = _ranked_keys_from_xml(result)

    report = evaluate_rankings(
        dataset,
        rankings,
        top_k_override=top_k,
    )
    report["dataset_sha256"] = dataset_sha256
    report["retrieval_config"] = {
        "mode": "page",
        "remote_query_embeddings": bool(allow_remote_embeddings),
    }
    return report
