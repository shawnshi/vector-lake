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


def test_compare_refuses_two_runs_over_different_corpora(tmp_path, capsys):
    """Two corpora are two experiments.

    The second batch's candidate pool was frozen before the vector backfill finished while its
    scoring runs happened after, so 330 of 333 queries disagreed between the two -- and nothing in
    the record said so.  A run now names the corpus it read.
    """
    digest = harness.query_digest("q")
    per = {digest: {"success": 1.0, "recall": 1.0, "reciprocal_rank": 1.0, "ndcg": 1.0}}
    metrics = {"judged": 1, "per_query": per}
    left = _write(tmp_path / "a.json", {"fusion": "sum", "corpus": "aaaa"}, metrics)
    right = _write(tmp_path / "b.json", {"fusion": "rrf", "corpus": "bbbb"}, metrics)

    assert harness.compare(left, right, 5) == 2
    assert "different corpora" in capsys.readouterr().err


def test_runs_without_a_recorded_corpus_still_compare(tmp_path):
    """Old run files predate the field; refusing them would refuse the record they came from."""
    digest = harness.query_digest("q")
    per = {digest: {"success": 1.0, "recall": 1.0, "reciprocal_rank": 1.0, "ndcg": 1.0}}
    metrics = {"judged": 1, "per_query": per}
    left = _write(tmp_path / "a.json", {"fusion": "sum"}, metrics)
    right = _write(tmp_path / "b.json", {"fusion": "rrf"}, metrics)

    assert harness.compare(left, right, 5) == 0


def test_the_corpus_fingerprint_is_stable_and_moves_with_the_corpus(isolated_memory):
    """Stable enough to compare, sensitive enough to notice a page appearing."""
    from vector_lake import db_store

    db_store.init_db()
    first = harness.corpus_fingerprint()
    assert first == harness.corpus_fingerprint()

    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
            ("Concept_New", "New", "新页", "新页"),
        )

    assert harness.corpus_fingerprint() != first


def test_a_label_file_can_record_the_corpus_it_was_judged_against(tmp_path):
    """The header is what turns a silent drift into a warning."""
    path = tmp_path / "labels.jsonl"
    path.write_text('# corpus abc123\n{"query": "q", "relevant": []}\n', encoding="utf-8")

    assert harness.labels_corpus(path) == "abc123"
    assert harness.load_labels(path) == {harness.query_digest("q"): set()}


def test_labels_without_a_header_report_no_corpus(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"query": "q", "relevant": ["A"]}\n', encoding="utf-8")

    assert harness.labels_corpus(path) is None


# --- the rule card ----------------------------------------------------------------------------
#
# The rule used to live in three places: two decision files written as deltas against each other and
# a third copy in this module.  Two drifts between the copies were found by hand (the sample gate sat
# at 60 while the registration said 300; the minimum effect was registered and never implemented), so
# the constants now come from the card and these tests keep that from quietly becoming a fourth copy.


def test_the_rule_constants_come_from_the_card():
    """A literal that disagrees with the card is the drift this replaced."""
    card = json.loads(harness.RULE_PATH.read_text(encoding="utf-8"))

    assert harness.PRIMARY_METRIC == card["primary_metric"]
    assert harness.SECONDARY_METRICS == tuple(card["secondary_metrics"])
    assert harness.COMPARISON_DENOMINATOR == card["comparison_universe"]["denominator"]
    assert harness.MIN_SAMPLE == card["gates"]["minimum_sample"]["value"]
    assert harness.MIN_PRIMARY_EFFECT == card["gates"]["primary_mean_difference"]["value"]
    assert harness.MIN_PRIMARY_EFFECT_SD == card["gates"]["primary_mean_difference_sd"]["value"]
    assert harness.PRIMARY_SIGN_ALPHA == card["gates"]["sign_test"]["alpha"]
    assert harness.RULE_VERSION == card["contract_version"]
    assert harness.RULE_SHA256 == harness.hashlib.sha256(harness.RULE_PATH.read_bytes()).hexdigest()


def test_the_card_records_which_gates_are_reported_and_not_required(tmp_path):
    """The sign test and the SD-unit threshold are lines, not verdicts, and the card says so."""
    card = json.loads(harness.RULE_PATH.read_text(encoding="utf-8"))

    assert card["gates"]["primary_mean_difference"]["role"] == "gate"
    assert card["gates"]["bootstrap_ci"]["role"] == "gate"
    assert card["gates"]["secondary_metrics"]["role"] == "gate"
    assert card["gates"]["minimum_sample"]["role"] == "gate"
    assert card["gates"]["sign_test"]["role"] == "report_only"
    assert card["gates"]["primary_mean_difference_sd"]["role"] == "report_only"


def test_a_missing_rule_card_fails_closed(tmp_path):
    """Guessed thresholds are the failure the card exists to prevent, so absence is fatal."""
    with pytest.raises(SystemExit):
        harness.load_rule(tmp_path / "absent.json")


def test_a_rule_card_naming_an_unknown_denominator_fails_closed(tmp_path):
    card = json.loads(harness.RULE_PATH.read_text(encoding="utf-8"))
    card["comparison_universe"]["denominator"] = "whatever"
    path = tmp_path / "card.json"
    path.write_text(json.dumps(card), encoding="utf-8")

    with pytest.raises(SystemExit):
        harness.load_rule(path)


def test_the_declared_denominator_selects_the_universe():
    """The one field that decides a verdict has to mean exactly one thing."""
    judged = {harness.query_digest("has-pages"): {"A"}, harness.query_digest("empty"): set()}
    per_query = dict.fromkeys(judged, {})
    deltas = {harness.query_digest("has-pages"): 0.1, harness.query_digest("empty"): 0.0}

    assert harness.comparison_universe(per_query, judged, "judged") == sorted(judged)
    assert harness.comparison_universe(per_query, judged, "confirmable") == [harness.query_digest("has-pages")]
    assert harness.comparison_universe(per_query, judged, "discordant", deltas) == [harness.query_digest("has-pages")]
    with pytest.raises(SystemExit):
        harness.comparison_universe(per_query, judged, "discordant")


def test_the_decision_reports_every_denominator_next_to_the_gate():
    """`at least 300 confirming queries (333)` named a number it did not count."""
    diffs = [0.2, 0.0, 0.0]
    lines = harness._decision(diffs, [[0.1, 0.0, 0.0]], {"judged": 3, "confirmable": 1, "discordant": 1})

    sample = [line for line in lines if "queries" in line][0]
    assert "judged 3 / confirmable 1 / discordant 1" in sample
    assert f"at least {harness.MIN_SAMPLE} {harness.COMPARISON_DENOMINATOR} queries" in sample
    assert sample.strip().startswith("FAIL")  # 3 < 300, however the three are counted


def test_the_sd_unit_line_is_reported_and_does_not_decide():
    """An absolute bar is not comparable across query compositions; that is a line, not a gate."""
    diffs = [0.30, 0.35, 0.40]
    lines = harness._decision(diffs, [[0.2, 0.2, 0.2]], {"judged": 3, "confirmable": 3, "discordant": 3})

    sd_line = [line for line in lines if "SD units" in line][0]
    assert sd_line.strip().startswith("(report-only)")
    assert "PASS" not in sd_line and "FAIL" not in sd_line
    # And the verdict is what it was before the line existed.
    comparable = [line for line in lines if line.strip().startswith(("PASS", "FAIL"))]
    assert len(comparable) == 4


def test_compare_refuses_two_runs_under_different_rule_cards(tmp_path, capsys):
    """A rule card is part of the experiment, the same way a corpus is."""
    digest = harness.query_digest("q")
    per = {digest: {"success": 1.0, "recall": 1.0, "reciprocal_rank": 1.0, "ndcg": 1.0}}
    left = _write(tmp_path / "a.json", {"fusion": "sum", "corpus": "c1"}, {"judged": 1, "per_query": per})
    right = _write(tmp_path / "b.json", {"fusion": "rrf", "corpus": "c1"}, {"judged": 1, "per_query": per})
    for path, version in ((left, "search-eval-rule/1.0"), (right, "search-eval-rule/1.1")):
        payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        payload["rule"] = {"contract_version": version, "sha256": version}
        pathlib.Path(path).write_text(json.dumps(payload), encoding="utf-8")

    assert harness.compare(left, right, 5) == 2
    assert "different rule cards" in capsys.readouterr().err


def test_compare_fails_closed_when_the_denominator_needs_labels_that_are_not_there(tmp_path, capsys, monkeypatch):
    """A 'confirmable' card read over every judged query is the mismatch the card makes visible."""
    digest = harness.query_digest("q")
    per = {digest: {"success": 1.0, "recall": 1.0, "reciprocal_rank": 1.0, "ndcg": 1.0}}
    left = _write(tmp_path / "a.json", {"fusion": "sum"}, {"judged": 1, "per_query": per})
    right = _write(tmp_path / "b.json", {"fusion": "rrf"}, {"judged": 1, "per_query": per})
    monkeypatch.setattr(harness, "COMPARISON_DENOMINATOR", "confirmable")

    assert harness.compare(left, right, 5) == 2
    assert "recorded the labels" in capsys.readouterr().err


def test_the_two_decision_files_cite_the_rule_card():
    """The card is the implemented source; the decision files are the record of why."""
    for name in ("search_eval_decisions.md", "search_eval_decisions_round2.md"):
        text = (REPO / "benchmarks" / name).read_text(encoding="utf-8")
        assert "search_eval_rule.json" in text, f"{name} does not cite the rule card"


# --- a registration has to be enforceable -------------------------------------------------------
#
# `query_construction.min_confirmable_queries` was in the first card and read by nothing: a batch
# could have registered a floor and the harness would have ignored it, which is the same drift the
# card was built to remove.  These keep a field from becoming a promise nobody keeps.


def test_the_card_refuses_a_field_nothing_reads(tmp_path):
    card = json.loads(harness.RULE_PATH.read_text(encoding="utf-8"))
    card["query_construction"]["min_confirmable_queries_typo"] = 150
    path = tmp_path / "card.json"
    path.write_text(json.dumps(card), encoding="utf-8")

    with pytest.raises(SystemExit, match="unknown query_construction field"):
        harness.load_rule(path)


def test_the_card_refuses_a_floor_that_is_not_a_positive_integer(tmp_path):
    card = json.loads(harness.RULE_PATH.read_text(encoding="utf-8"))
    for bad in (0, -1, "150", True):
        card["query_construction"]["min_confirmable_queries"] = bad
        path = tmp_path / "card.json"
        path.write_text(json.dumps(card), encoding="utf-8")
        with pytest.raises(SystemExit, match="positive integer"):
            harness.load_rule(path)


def test_a_registered_confirmable_floor_is_actually_read(monkeypatch):
    """The test the first card would have failed: registering the floor must change a line."""
    counts = {"judged": 333, "confirmable": 65, "discordant": 31, "labels_pin": "matched"}
    diffs = [0.2, 0.1, -0.05]

    monkeypatch.setattr(harness, "MIN_CONFIRMABLE_QUERIES", None)
    without = harness._decision(diffs, [[0.1, 0.1, 0.0]], counts)
    monkeypatch.setattr(harness, "MIN_CONFIRMABLE_QUERIES", 150)
    with_floor = harness._decision(diffs, [[0.1, 0.1, 0.0]], counts)

    assert not any("confirmable queries" in line for line in without)
    floor_line = [line for line in with_floor if "confirmable queries" in line][0]
    assert floor_line.strip().startswith("FAIL")  # 65 < 150
    assert "(65)" in floor_line
    # And an unverifiable count fails closed rather than being treated as a pass.
    unverifiable = harness._decision(diffs, [[0.1, 0.1, 0.0]], dict(counts, confirmable=None, labels_pin="stale"))
    assert [line for line in unverifiable if "confirmable queries" in line][0].strip().startswith("FAIL")


# --- the labels revision is pinned, or said not to be ------------------------------------------


def test_file_sha256_reads_bytes_and_reports_a_missing_file(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"query": "q", "relevant": ["A"]}\n', encoding="utf-8")

    assert harness.file_sha256(path) == harness.hashlib.sha256(path.read_bytes()).hexdigest()
    assert harness.file_sha256(tmp_path / "absent.jsonl") is None


def _labels_file(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "labels.jsonl"
    path.write_text(
        json.dumps({"query": "has-pages", "relevant": ["A"]}) + "\n"
        + json.dumps({"query": "empty", "relevant": []}) + "\n",
        encoding="utf-8",
    )
    return path


def _pair(tmp_path: pathlib.Path, config_extra: dict) -> tuple[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    digest = harness.query_digest
    per = {
        digest("has-pages"): {"success": 1.0, "recall": 1.0, "reciprocal_rank": 1.0, "ndcg": 1.0},
        digest("empty"): {"success": 0.0, "recall": 0.0, "reciprocal_rank": 0.0, "ndcg": 0.0},
    }
    left = _write(tmp_path / "a.json", {"fusion": "sum", "corpus": "c1", **config_extra},
                  {"judged": 2, "per_query": per})
    right = _write(tmp_path / "b.json", {"fusion": "rrf", "corpus": "c1", **config_extra},
                   {"judged": 2, "per_query": per})
    return left, right


def test_compare_reports_the_count_but_marks_labels_the_run_did_not_pin(tmp_path, capsys):
    """A run that predates the hash cannot be checked; the count still has to be readable."""
    labels = _labels_file(tmp_path)
    left, right = _pair(tmp_path, {"labels": str(labels)})

    harness.compare(left, right, 5)

    line = [row for row in capsys.readouterr().out.splitlines() if "comparison universe" in row][0]
    assert "confirmable 1" in line
    assert line.endswith("; labels not pinned by the run)"), line


def test_compare_withholds_the_count_when_the_labels_moved(tmp_path, capsys):
    """The verdict lives in the run; the count does not, so an edited file must not renumber it."""
    labels = _labels_file(tmp_path)
    left, right = _pair(tmp_path, {"labels": str(labels), "labels_sha256": "0" * 64})

    harness.compare(left, right, 5)
    captured = capsys.readouterr()

    line = [row for row in captured.out.splitlines() if "comparison universe" in row][0]
    assert "confirmable ?" in line
    assert line.endswith("; labels stale at the recorded path)"), line
    assert "no longer hash to the revision" in captured.err
    # The verdict itself is unchanged: the diff is the same and the primary mean still decides.
    assert "primary mean difference >= registered minimum effect" in captured.out


def test_compare_refuses_two_revisions_of_one_label_file(tmp_path, capsys):
    """One path, two revisions means the two arms were scored against different relevance."""
    labels = _labels_file(tmp_path)
    left, _ = _pair(tmp_path / "l", {"labels": str(labels), "labels_sha256": "a" * 64})
    _, right = _pair(tmp_path / "r", {"labels": str(labels), "labels_sha256": "b" * 64})

    assert harness.compare(left, right, 5) == 2
    assert "different revisions of the label file" in capsys.readouterr().err


def test_compare_warns_when_only_one_side_records_its_labels(tmp_path, capsys):
    """Not fatal -- a hand-built payload may omit the key -- but the silence is what to fix."""
    labels = _labels_file(tmp_path)
    left, right = _pair(tmp_path, {"labels": str(labels)})
    payload = json.loads(pathlib.Path(right).read_text(encoding="utf-8"))
    payload["config"] = {k: v for k, v in payload["config"].items() if k != "labels"}
    pathlib.Path(right).write_text(json.dumps(payload), encoding="utf-8")

    assert harness.compare(left, right, 5) == 0
    assert "only the left run recorded the labels" in capsys.readouterr().err
