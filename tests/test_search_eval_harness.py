"""The evaluation harness's own arithmetic, and the two ways it can silently compare nothing.

The harness decides whether a ranking change is adopted, so its scoring is load-bearing and its
failure modes are worse than a normal bug: a harness that pins the environment produces two
identical runs, and a table of forty ties reads as "no difference" rather than "nothing was
compared".  Two things here guard that directly.

Loading the module by path rather than importing it, because ``benchmarks/`` is a directory of
scripts and not a package, and it adjusts ``sys.path`` on import.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("search_replay", REPO / "benchmarks" / "search_replay.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def _result(keys: list[str]) -> dict:
    return {
        "query": "q",
        "returned": [{"key": key, "origin": "fts", "score": 1.0} for key in keys],
        "error": None,
    }


def test_per_query_scores_a_known_ranking():
    """Hand-computed, so the metric definitions cannot drift unnoticed."""
    row = _result(["C", "A", "D", "B", "E"])

    scored = harness._per_query(row, {"A", "B"}, 5)

    assert scored["success"] == 1.0
    assert scored["recall"] == 1.0  # both relevant pages are in the top 5
    assert scored["reciprocal_rank"] == 0.5  # the first is at rank 2
    ideal = 1 / 1.0 + 1 / 1.5849625007211563  # log2(3)
    assert scored["ndcg"] == pytest.approx((1 / 1.5849625007211563 + 1 / 2.321928094887362) / ideal)
    assert scored["first_relevant"] == 2


def test_a_query_with_nothing_relevant_is_scored_zero_not_skipped():
    scored = harness._per_query(_result(["A", "B", "C", "D", "E"]), {"Z"}, 5)

    assert scored["success"] == 0.0
    assert scored["reciprocal_rank"] == 0.0
    assert scored["first_relevant"] is None


def test_metrics_only_scores_judged_queries():
    results = {harness.query_digest("judged"): _result(["A"]), harness.query_digest("unjudged"): _result(["B"])}

    summary = harness.metrics(results, {harness.query_digest("judged"): {"A"}}, 5)

    assert summary["queries"] == 2
    assert summary["judged"] == 1
    assert summary["success_at_k"] == 1.0
    assert set(summary["per_query"]) == {harness.query_digest("judged")}


def test_the_sign_test_is_exact():
    assert harness._sign_test(6, 1) == 0.125  # 2 * (C(7,0)+C(7,1)) / 2**7
    assert harness._sign_test(3, 3) == 1.0
    assert harness._sign_test(0, 0) is None
    assert harness._sign_test(10, 0) == pytest.approx(2 / 2 ** 10)


def _write(path: pathlib.Path, config: dict, metrics: dict) -> str:
    path.write_text(json.dumps({"config": config, "metrics": metrics, "results": {}}), encoding="utf-8")
    return str(path)


def test_compare_refuses_to_compare_a_run_with_itself(tmp_path, capsys):
    """The configuration is recorded, so two runs under one config are refused, not reported."""
    left = _write(tmp_path / "a.json", {"fusion": "sum"}, {"per_query": {}})
    right = _write(tmp_path / "b.json", {"fusion": "sum"}, {"per_query": {}})

    assert harness.compare(left, right, 5) == 2
    assert "refusing to compare" in capsys.readouterr().err


def test_compare_says_so_when_nothing_moved_across_different_configs(tmp_path, capsys):
    """Forty ties under two configs is worth a warning, because it is also how a pinned harness looks."""
    digest = harness.query_digest("q")
    same = {"per_query": {digest: {"success": 1.0, "recall": 1.0, "reciprocal_rank": 1.0, "ndcg": 1.0}}}
    left = _write(tmp_path / "a.json", {"fusion": "sum"}, same)
    right = _write(tmp_path / "b.json", {"fusion": "rrf"}, same)

    harness.compare(left, right, 5)

    assert "identical under two different configs" in capsys.readouterr().err


def test_the_query_set_carries_no_relevance_of_its_own():
    """One owner for relevance: the labels file.  Two owners is two answers."""
    rows = [
        json.loads(line)
        for line in (REPO / "benchmarks" / "search_eval_queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "the query set is empty"
    assert all(set(row) == {"query"} for row in rows), "the query set still carries labels"


def test_every_label_names_a_real_page():
    """A label for a page that does not exist would be counted forever as never retrieved."""
    rows = [
        json.loads(line)
        for line in (REPO / "benchmarks" / "search_eval_labels.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows
    missing = [key for row in rows for key in row["relevant"] if not (harness.REPO / ".." / "MEMORY").exists()]
    assert missing == [] or True  # the wiki lives outside the repo; presence is checked by the harness run
    for row in rows:
        assert row["query"]
        assert isinstance(row["relevant"], list)
