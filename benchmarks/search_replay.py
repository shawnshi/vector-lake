#!/usr/bin/env python3
"""Replay a fixed query set, score it against judged labels, and compare two runs.

The A1/A2 ranking work changes which candidates reach ``top_k``, and this lake keeps no record that
could show whether a change improved retrieval or damaged it: there is no ``log.md`` and no query
table.  ``search_ledger`` records production answers as digests, which answers "is the same query
answering differently" but not "is the answer better".  This harness supplies the other half.

Three modes:

    replay   (default) run the set and score it against ``search_eval_labels.jsonl``
    --pool   run every fusion under comparison and print their union, *blinded*, for judging
    --compare A B   paired comparison: per-query deltas, win/loss/tie, sign test, bootstrap CI

Judging method, because the metric is only as good as this part:

* the pool is the union of every configuration's top-k, so a document either system ranks in the top
  k is judged.  Pooling at the *same depth as the metric* is what makes that exact for recall@k and
  MRR@k: nothing outside the pool can affect either.
* the listing is sorted by page key and shows no scores and no origins, so the judgement cannot see
  which system proposed what.  Judging from a system's own output would just re-score that system.
* a judged-relevant page is one whose content answers the query, most often because the page *is*
  what the query names.  Unjudged documents count as not relevant -- the standard pool assumption,
  and the reason the pool depth has to match the metric depth.

Vector source (the vector path needs a query embedding, which is a provider round trip):

    snapshot  (default) a JSON file under ``<meta>/runtime/``; created on first run
    live      call the provider and refresh the snapshot
    stored    one stored vector for every query -- offline, deterministic, and *not* a measure of
              retrieval quality; it exercises the fusion mechanics only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import random
import sqlite3
import struct
import sys
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from vector_lake import db_store, tool_search  # noqa: E402
from vector_lake.wiki_utils import get_meta_dir  # noqa: E402

try:  # absent before the ledger landed; the harness must also run against an older tree
    from vector_lake import search_ledger  # noqa: F811
except ImportError:  # pragma: no cover - only reachable when replaying an older checkout
    search_ledger = None

QUERY_SET = REPO / "benchmarks" / "search_eval_queries.jsonl"
LABEL_SET = REPO / "benchmarks" / "search_eval_labels.jsonl"
SNAPSHOT_NAME = "search_eval_vectors.json"
#: Fusions the pool is built from, so a comparison cannot be biased by judging only one side's
#: candidates.
POOL_FUSIONS = ("sum", "rrf")
BOOTSTRAP_RESAMPLES = 20000
BOOTSTRAP_SEED = 20260920


def query_digest(query: str) -> str:
    """Prefers the ledger's digest so a replay keys match production entries."""
    if search_ledger is not None:
        return search_ledger.query_digest(query)
    import hashlib

    return hashlib.sha256(str(query).encode("utf-8")).hexdigest()[:16]


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(json.loads(line))
    return rows


def load_queries(path: pathlib.Path, limit: int = 0) -> list[dict]:
    queries = _read_jsonl(path)
    return queries[:limit] if limit else queries


def load_labels(path: pathlib.Path) -> dict[str, set[str]]:
    """``{query_digest: {relevant page keys}}`` -- the only owner of relevance judgement.

    The query set deliberately carries no ``expect`` field: two places to record what is relevant
    is two places to disagree, and only this file should be able to move a metric.
    """
    if not path.exists():
        return {}
    return {
        query_digest(row["query"]): set(row.get("relevant") or [])
        for row in _read_jsonl(path)
    }


def snapshot_path() -> pathlib.Path:
    return get_meta_dir() / "runtime" / SNAPSHOT_NAME


def corpus_fingerprint() -> str:
    """A short, stable digest of the *corpus* a run retrieves from.

    The query vectors are pinned in a snapshot; the corpus was not pinned at all, and it moved under
    a live evaluation: the second batch's candidate pool was frozen while the vector projection still
    held 2602 of what became 7175 vectors, and the scoring runs happened after the backfill filled
    it in.  Measured afterwards: the frozen pool and the same-day k=5 runs disagreed for 330 of 333
    queries (664 keys returned that the pool never saw, 1348 pool keys no longer returned), and 97.6%
    of the new keys arrived through the vector arm.  The relative comparison survived -- both fusions
    were scored against the same runs and labels -- but its absolute level and its coverage numbers
    described the older index, not the system under test.

    This digest covers what retrieval actually reads: which pages exist, which of them have vectors,
    and how big the full-text index is.  It is a fingerprint of the corpus, not of its contents, and
    it costs two counts and one key scan.
    """
    conn = db_store.get_connection()
    digest = hashlib.sha256()
    for table, column in (("page_index_nodes", "node_key"),
                          ("vec_embeddings", "page_key"),
                          ("wiki_search_index", "node_key")):
        try:
            rows = conn.execute(f"select {column} from {table} order by {column}").fetchall()
        except sqlite3.Error as exc:
            digest.update(f"{table}:unavailable:{exc}".encode())
            continue
        digest.update(f"{table}:{len(rows)}".encode())
        for row in rows:
            digest.update(str(row[0]).encode("utf-8"))
    return digest.hexdigest()[:12]


def labels_corpus(path: pathlib.Path) -> str | None:
    """The corpus a label set was judged against, if its header records one."""
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("# corpus "):
                return line.strip().split(" ", 2)[2]
            if not line.startswith("#"):
                return None
    return None


def _stored_vector() -> list[float]:
    """One row of the vector projection, chosen deterministically, for the offline mode."""
    conn = db_store.get_connection()
    row = conn.execute("select embedding from vec_embeddings order by page_key limit 1").fetchone()
    if row is None:
        raise SystemExit("vec_embeddings is empty; use --vectors live or run embedding-backfill")
    blob = row["embedding"]
    dim = len(blob) // 4
    return list(struct.unpack(f"<{dim}f", blob[: dim * 4]))


def build_vectors(queries: list[dict], mode: str) -> dict[str, list[float]]:
    """``{query_digest: vector}`` for every query in the set, per ``mode``."""
    path = snapshot_path()
    if mode == "stored":
        vector = _stored_vector()
        return {query_digest(item["query"]): vector for item in queries}

    cached: dict[str, list[float]] = {}
    if mode == "snapshot" and path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))

    missing = [item["query"] for item in queries if query_digest(item["query"]) not in cached]
    if mode == "live" or missing:
        if missing and mode == "snapshot":
            print(f"[vectors] {len(missing)} quer{'y' if len(missing) == 1 else 'ies'} not in the "
                  f"snapshot; fetching from the provider", file=sys.stderr)
        for query in missing:
            vector, reason = tool_search._get_query_embedding(query)
            if not vector:
                print(f"[vectors] unavailable for one query: {reason}; using a stored vector",
                      file=sys.stderr)
                vector = _stored_vector()
            cached[query_digest(query)] = vector
        if mode in {"snapshot", "live"}:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached), encoding="utf-8")
            print(f"[vectors] snapshot written: {path} ({len(cached)} vectors)", file=sys.stderr)

    return cached


#: Distinct from ``None``, which is a fusion a caller could legitimately ask for as "unset":
#: the sentinel means "do not touch the environment at all".
_UNSET = object()


def run(queries: list[dict], vectors: dict[str, list[float]], top_k: int, fusion=_UNSET) -> dict:
    """Replay every query.

    ``fusion`` is the sentinel by default, and that matters: the replay path must use whatever the
    environment says, because that is how ``VECTOR_LAKE_FUSION=rrf python search_replay.py`` selects
    a configuration.  An earlier version of this function cleared the variable when no fusion was
    passed, which silently pinned every replay to ``sum`` -- two "different" runs then produced
    byte-identical metrics, and the comparison reported 40 ties.
    """
    previous = os.environ.get("VECTOR_LAKE_FUSION")
    touched = fusion is not _UNSET
    if touched:
        if fusion is None:
            os.environ.pop("VECTOR_LAKE_FUSION", None)
        else:
            os.environ["VECTOR_LAKE_FUSION"] = fusion
    #: A replay is a measurement, not operator traffic, so the ledger stays out of it.  This guard
    #: belongs around the retrieval loop rather than in ``main()`` or at import: ``main()`` is not the
    #: only entry point (measurement scripts import ``run()`` directly, and doing so wrote 3071 rows --
    #: all 333 distinct queries in the live ledger were batch-1 or batch-2 queries, so it held no
    #: operator traffic at all), and setting it at import turned the ledger off process-wide, which
    #: broke its own tests.  Scoped here, it lasts exactly as long as the replay and leaves an
    #: explicit ``VECTOR_LAKE_SEARCH_LEDGER=1`` alone.
    ledger_previous = os.environ.get("VECTOR_LAKE_SEARCH_LEDGER")
    if ledger_previous is None:
        os.environ["VECTOR_LAKE_SEARCH_LEDGER"] = "0"
    try:
        results = {}
        for item in queries:
            query = item["query"]
            digest = query_digest(query)
            vector = vectors.get(digest) or []
            with mock.patch.object(tool_search, "_get_query_embedding", lambda q, v=vector: (v, None)):
                final, notes, error = tool_search._search_scored_pages(query, top_k=top_k)
            results[digest] = {
                "query": query,
                "returned": [
                    {"key": node["_key"], "origin": node.get("_origin", "?"), "score": round(score, 6)}
                    for score, node in final
                ],
                "notes": list(notes or []),
                "error": error,
            }
        return results
    finally:
        if touched:
            if previous is None:
                os.environ.pop("VECTOR_LAKE_FUSION", None)
            else:
                os.environ["VECTOR_LAKE_FUSION"] = previous
        if ledger_previous is None:
            os.environ.pop("VECTOR_LAKE_SEARCH_LEDGER", None)


# --- scoring -----------------------------------------------------------------------------------


def _per_query(row: dict, relevant: set[str], top_k: int) -> dict:
    """The four per-query numbers the comparison is paired on."""
    keys = [entry["key"] for entry in row["returned"]][:top_k]
    positions = [index + 1 for index, key in enumerate(keys) if key in relevant]
    first = min(positions) if positions else None
    gains = [1.0 if key in relevant else 0.0 for key in keys]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(relevant), top_k)))
    return {
        "success": 1.0 if first else 0.0,
        "recall": (len(set(keys) & relevant) / len(relevant)) if relevant else 0.0,
        "reciprocal_rank": (1.0 / first) if first else 0.0,
        "ndcg": (dcg / ideal) if ideal else 0.0,
        "first_relevant": first,
    }


def metrics(results: dict, labels: dict[str, set[str]], top_k: int) -> dict:
    judged = {digest: labels[digest] for digest in results if digest in labels}
    per_query = {digest: _per_query(results[digest], relevant, top_k) for digest, relevant in judged.items()}
    origins: dict[str, int] = {}
    for row in results.values():
        for entry in row["returned"]:
            origins[entry["origin"]] = origins.get(entry["origin"], 0) + 1

    def mean(field: str) -> float | None:
        if not per_query:
            return None
        return round(sum(row[field] for row in per_query.values()) / len(per_query), 4)

    return {
        "queries": len(results),
        "judged": len(judged),
        "relevant_pages": sum(len(relevant) for relevant in judged.values()),
        "success_at_k": mean("success"),
        "recall_at_k": mean("recall"),
        "mrr": mean("reciprocal_rank"),
        "ndcg_at_k": mean("ndcg"),
        "origins": dict(sorted(origins.items())),
        "errors": sum(1 for row in results.values() if row.get("error")),
        "per_query": per_query,
    }


# --- comparison --------------------------------------------------------------------------------


def _sign_test(wins: int, losses: int) -> float | None:
    """Two-sided exact binomial p for ``wins`` vs ``losses``, ties excluded.  No dependencies.

    Unrounded: a p-value that has been rounded to four places cannot be tested against its own
    definition, and ``_sign_test(10, 0)`` rounds to the same 0.002 as several neighbours.
    """
    trials = wins + losses
    if trials == 0:
        return None
    tail = sum(math.comb(trials, index) for index in range(0, min(wins, losses) + 1))
    return min(1.0, 2.0 * tail / 2 ** trials)


def _bootstrap_ci(differences: list[float]) -> tuple[float, float, float] | None:
    """Paired bootstrap of the mean difference: ``(low, high, p_one_sided)``, deterministic seed."""
    if not differences:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    count = len(differences)
    means = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        means.append(sum(differences[rng.randrange(count)] for _ in range(count)) / count)
    means.sort()
    low = means[int(0.025 * len(means))]
    high = means[int(0.975 * len(means)) - 1]
    non_positive = sum(1 for value in means if value <= 0) / len(means)
    return round(low, 4), round(high, 4), min(non_positive, 1 - non_positive) * 2


#: The pre-registered decision rule has one implemented home: ``benchmarks/search_eval_rule.json``.
#: This module reads it rather than restating it, because the restatement is what drifted twice: the
#: sample gate sat at batch 1's 60 while batch 2's registration said 300, and the minimum effect was
#: registered and never implemented.  Both were found by reading the two decision files by hand.
#:
#: The card also names the **comparison universe** (which queries the paired comparison is read
#: over), because that one field decides a verdict: read over every judged query, batch 2's primary
#: difference is +0.0132 and fails the +0.05 bar; read over the 65 queries that have a page to find,
#: it is +0.0675 and passes it, while 65 fails the sample gate.  Both batches were read over
#: ``judged``, and the card keeps that so no verdict moves retroactively -- see
#: ``comparison_universe.note`` and ``effect_calibration`` in the card.
#:
#: PRIMARY_METRIC is nDCG@5 because it penalises both ways the two fusions trade off (a query's own
#: page not ranking first, and later relevant pages being pushed out), while MRR sees only the first.
#: The sign test is reported and not required: the first batch showed it reads only signs, needs ~780
#: discordant pairs at the observed win rate, and is therefore almost impossible to pass even when
#: the effect is real, so the second batch made the bootstrap interval the only significance gate.
RULE_PATH = REPO / "benchmarks" / "search_eval_rule.json"


def _fts_backend() -> str:
    """Which lexical backend this run actually uses, recorded in the run config.

    Read through ``tantivy_index.enabled()`` rather than straight from the environment so the label
    matches what the code would do -- an uninstalled wheel falls back to FTS5 with a warning, and a
    run that fell back must not claim to be a tantivy run.
    """
    try:
        from vector_lake import tantivy_index

        return "tantivy" if tantivy_index.enabled() else "fts5"
    except Exception:  # noqa: BLE001 - an absent module means the FTS5 path
        return "fts5"


def load_rule(path: pathlib.Path) -> dict:
    """Read the rule card, or fail closed.

    A missing or malformed card stops the harness instead of falling back to defaults: thresholds
    that were guessed are the failure this card exists to prevent, and a silent fallback would also
    hide a card that no longer matches the code that reads it.
    """
    try:
        rule = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"search eval rule card is missing: {path} ({exc})") from exc
    except ValueError as exc:
        raise SystemExit(f"search eval rule card is not valid JSON: {path} ({exc})") from exc
    required = ("contract_version", "primary_metric", "secondary_metrics", "comparison_universe", "gates")
    missing = [key for key in required if key not in rule]
    if missing:
        raise SystemExit(f"search eval rule card is missing {missing}: {path}")
    gate_keys = ("primary_mean_difference", "bootstrap_ci", "secondary_metrics", "minimum_sample")
    absent = [key for key in gate_keys if key not in rule["gates"]]
    if absent:
        raise SystemExit(f"search eval rule card is missing gate(s) {absent}: {path}")
    universe = rule["comparison_universe"]
    if universe.get("denominator") not in (universe.get("allowed") or []):
        raise SystemExit(
            f"search eval rule card declares denominator {universe.get('denominator')!r}, "
            f"which is not one of {universe.get('allowed')}: {path}"
        )
    if rule["gates"]["minimum_sample"].get("denominator") != universe.get("denominator") and \
            rule["gates"]["minimum_sample"].get("denominator") not in (universe.get("allowed") or []):
        raise SystemExit(f"search eval rule card sample denominator is unknown: {path}")
    # A field nothing reads is a registration that cannot be enforced: batch 2's floor could have
    # been set on this card and silently ignored.  Adding one means adding its consumer and this
    # list, which keeps that visible instead of discovering it a batch later.
    known = {
        "comparison_universe": {
            "denominator", "allowed", "definition", "applies_to", "does_not_apply_to", "note",
        },
        "query_construction": {"min_new_queries", "min_confirmable_queries", "note"},
    }
    for section, allowed in known.items():
        unknown = set(rule.get(section) or {}) - allowed
        if unknown:
            raise SystemExit(
                f"search eval rule card has unknown {section} field(s) {sorted(unknown)}: a field "
                f"nothing reads cannot be enforced, so add its consumer and this list: {path}"
            )
    floor = (rule.get("query_construction") or {}).get("min_confirmable_queries")
    if floor is not None and (not isinstance(floor, int) or isinstance(floor, bool) or floor < 1):
        raise SystemExit(
            f"search eval rule card min_confirmable_queries is not a positive integer: {path}"
        )
    return rule


RULE = load_rule(RULE_PATH)
RULE_SHA256 = hashlib.sha256(RULE_PATH.read_bytes()).hexdigest()
RULE_VERSION = str(RULE["contract_version"])
PRIMARY_METRIC = str(RULE["primary_metric"])
SECONDARY_METRICS = tuple(RULE["secondary_metrics"])
COMPARISON_DENOMINATOR = str(RULE["comparison_universe"]["denominator"])
SAMPLE_DENOMINATOR = str(RULE["gates"]["minimum_sample"]["denominator"])
MIN_SAMPLE = int(RULE["gates"]["minimum_sample"]["value"])
MIN_CONFIRMABLE_QUERIES = (RULE.get("query_construction") or {}).get("min_confirmable_queries")
MIN_PRIMARY_EFFECT = float(RULE["gates"]["primary_mean_difference"]["value"])
MIN_PRIMARY_EFFECT_SD = float(RULE["gates"]["primary_mean_difference_sd"]["value"])
#: Which of the two effect units actually gates.  Read from the card rather than decided here: from
#: 1.2 the absolute bar is reported and the SD bar gates, because one absolute value is not comparable
#: across query compositions (0.18 SD on batch 1, 0.41 SD on batch 2).  A future rule change that
#: moves the role back must not need a code change.
PRIMARY_EFFECT_ROLE = str(RULE["gates"]["primary_mean_difference"].get("role", "gate"))
PRIMARY_EFFECT_SD_ROLE = str(RULE["gates"]["primary_mean_difference_sd"].get("role", "report_only"))
PRIMARY_SIGN_ALPHA = float(RULE["gates"]["sign_test"]["alpha"])


def file_sha256(path: pathlib.Path) -> str | None:
    """The bytes digest of a file, or ``None`` when it cannot be read.

    Recorded for the labels a run was scored against.  Without it, the count of queries that could
    have differed is read from whatever sits at that path *now*, so a file edited after the run
    changes a number the run is reported with -- and no verdict input, which stays inside the run.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def rule_provenance() -> dict:
    """What ran, recorded in every payload so two runs can be told apart by rule, not only by config."""
    return {
        "contract_version": RULE_VERSION,
        "path": RULE_PATH.relative_to(REPO).as_posix(),
        "sha256": RULE_SHA256,
        "comparison_denominator": COMPARISON_DENOMINATOR,
    }


def comparison_universe(
    per_query: dict,
    labels: dict[str, set[str]],
    denominator: str,
    deltas: dict[str, float] | None = None,
) -> list[str]:
    """The queries the paired comparison is read over, per the card's declared denominator.

    ``judged`` is every query with a result in both runs; ``confirmable`` drops the ones whose
    relevant set is empty, i.e. the queries where no arm could ever differ; ``discordant`` keeps
    only the queries where the two arms actually differ.  The level metrics in :func:`metrics` do
    not use this: a query with nothing to find still measures the retriever, which is a different
    question from whether the two arms differ.
    """
    universe = sorted(per_query)
    if denominator == "judged":
        return universe
    if denominator == "confirmable":
        return [digest for digest in universe if labels.get(digest)]
    if denominator == "discordant":
        if deltas is None:
            raise SystemExit(
                "the 'discordant' denominator needs the paired differences; call it once both "
                "runs are scored"
            )
        return [digest for digest in universe if abs(deltas.get(digest, 0.0)) > 1e-9]
    raise SystemExit(f"unknown comparison denominator {denominator!r}; see {RULE_PATH.name}")


def _decision(diffs: list[float], secondary: list[list[float]], counts: dict | None = None) -> list[str]:
    """Evaluate the rule card.  Returns one line per condition, pass or fail.

    ``counts`` carries the three candidate denominators so the sample line can report the ones the
    gate did not use.  Without it the line still evaluates the gate the run recorded; it just cannot
    say how many of those queries had a page to find, which is the number the old line named.
    """
    wins = sum(1 for value in diffs if value > 1e-9)
    losses = sum(1 for value in diffs if value < -1e-9)
    mean = sum(diffs) / len(diffs) if diffs else 0.0
    variance = sum((value - mean) ** 2 for value in diffs) / (len(diffs) - 1) if len(diffs) > 1 else 0.0
    sd = math.sqrt(variance)
    ci = _bootstrap_ci(diffs)
    sign = _sign_test(wins, losses) if (wins + losses) else None
    lines = []
    # Both units are always printed; the card decides which one decides.  Reading the role instead of
    # hard-coding it here is what keeps this module an implementation of the rule rather than a second
    # copy of it -- the failure this whole card exists to prevent.
    ratio = (mean / sd) if sd else 0.0
    bar_in_sd = (MIN_PRIMARY_EFFECT / sd) if sd else 0.0
    if PRIMARY_EFFECT_ROLE == "gate":
        lines.append(f"  {'PASS' if mean >= MIN_PRIMARY_EFFECT else 'FAIL'}  "
                     f"primary mean difference >= registered minimum effect  "
                     f"({mean:+.4f} vs {MIN_PRIMARY_EFFECT:+.2f})")
    else:
        lines.append(f"  (report-only)  primary mean difference >= {MIN_PRIMARY_EFFECT:+.2f} absolute  "
                     f"({mean:+.4f})")
    if PRIMARY_EFFECT_SD_ROLE == "gate":
        lines.append(f"  {'PASS' if ratio >= MIN_PRIMARY_EFFECT_SD else 'FAIL'}  "
                     f"primary mean difference >= registered minimum effect in SD units  "
                     f"({ratio:+.3f} vs {MIN_PRIMARY_EFFECT_SD:+.2f} SD; the {MIN_PRIMARY_EFFECT:+.2f} "
                     f"absolute bar is {bar_in_sd:+.3f} SD on this composition)")
    else:
        lines.append(f"  (report-only)  primary mean difference in SD units  "
                     f"({ratio:+.3f} observed; the {MIN_PRIMARY_EFFECT:+.2f} absolute bar is "
                     f"{bar_in_sd:+.3f} SD on this composition, registered bar "
                     f"{MIN_PRIMARY_EFFECT_SD:+.2f} SD)")
    excluded = ci is not None and ci[0] > 0
    lines.append(f"  {'PASS' if excluded else 'FAIL'}  primary bootstrap 95% CI excludes 0  "
                 f"({'[%+.3f, %+.3f]' % (ci[0], ci[1]) if ci else '-'})")
    # Report-only since the second batch; see the note on this module's rule constants.
    lines.append(f"  (report-only)  primary sign test p < {PRIMARY_SIGN_ALPHA}  "
                 f"({sign:.4f} on {wins} wins / {losses} losses)" if sign is not None
                 else f"  (report-only)  primary sign test  (no discordant pair)")
    regressions = [
        name for name, values in zip(SECONDARY_METRICS, secondary)
        if values and sum(values) / len(values) < 0
    ]
    lines.append(f"  {'PASS' if not regressions else 'FAIL'}  no secondary metric regresses  "
                 f"({', '.join(regressions) if regressions else 'none'})")
    counts = counts or {}
    breakdown = " / ".join(
        f"{name} {counts.get(name, '?')}" for name in ("judged", "confirmable", "discordant")
    )
    lines.append(f"  {'PASS' if len(diffs) >= MIN_SAMPLE else 'FAIL'}  "
                 f"at least {MIN_SAMPLE} {COMPARISON_DENOMINATOR} queries  "
                 f"({len(diffs)}; {breakdown})")
    if MIN_CONFIRMABLE_QUERIES is not None:
        # Registered and inert only while null.  It cannot be read off the run: the count lives in
        # the labels, so an unverifiable or stale revision fails closed instead of guessing.
        confirmable = counts.get("confirmable")
        pin = counts.get("labels_pin")
        if confirmable is None or pin == "stale":
            lines.append(f"  FAIL  at least {MIN_CONFIRMABLE_QUERIES} confirmable queries  "
                         f"(unverifiable: the labels are {pin or 'missing'})")
        else:
            lines.append(f"  {'PASS' if confirmable >= MIN_CONFIRMABLE_QUERIES else 'FAIL'}  "
                         f"at least {MIN_CONFIRMABLE_QUERIES} confirmable queries  "
                         f"({confirmable}{'' if pin == 'matched' else ', labels not pinned'})")
    return lines


def compare(left_path: str, right_path: str, top_k: int) -> int:
    left = json.loads(pathlib.Path(left_path).read_text(encoding="utf-8"))
    right = json.loads(pathlib.Path(right_path).read_text(encoding="utf-8"))
    if left.get("config") == right.get("config"):
        # The failure this catches: a harness that pins the environment produces two identical runs
        # and a table of 40 ties, which reads as "no difference" rather than "nothing was compared".
        print(f"refusing to compare: both runs used the same config {left.get('config')}", file=sys.stderr)
        return 2
    for side, name in ((left, "left"), (right, "right")):
        # The sibling failure of the one below: comparing a real run against one that judged nothing
        # reads as "identical", which is the same wrong answer as "no difference".  Only an explicit
        # zero counts -- a hand-built payload without the key is not a claims about judging nothing.
        if side.get("metrics", {}).get("judged") == 0:
            print(f"refusing to compare: the {name} run judged no queries (judged=0)", file=sys.stderr)
            return 2
    left_corpus = (left.get("config") or {}).get("corpus")
    right_corpus = (right.get("config") or {}).get("corpus")
    if left_corpus and right_corpus and left_corpus != right_corpus:
        # Two runs over two different corpora are two experiments, not two arms of one.
        print(f"refusing to compare: different corpora ({left_corpus} vs {right_corpus})",
              file=sys.stderr)
        return 2
    left_rule = (left.get("rule") or {}).get("sha256")
    right_rule = (right.get("rule") or {}).get("sha256")
    if left_rule and right_rule and left_rule != right_rule:
        # Same argument as the corpus gate: a rule card is part of the experiment, and a run read
        # under a different card was read under a different rule, not under the same one twice.
        print(f"refusing to compare: the runs were scored under different rule cards "
              f"({(left.get('rule') or {}).get('contract_version')} vs "
              f"{(right.get('rule') or {}).get('contract_version')})", file=sys.stderr)
        return 2
    left_labels_path = (left.get("config") or {}).get("labels")
    right_labels_path = (right.get("config") or {}).get("labels")
    if bool(left_labels_path) != bool(right_labels_path):
        # Not fatal, because a hand-built payload is allowed to omit the key, but it is worth
        # saying: the side that stayed silent may have been scored against something else.
        only = left_labels_path or right_labels_path
        side = "left" if left_labels_path else "right"
        print(f"warning: only the {side} run recorded the labels it was scored against ({only}); "
              f"the other run's relevance is unknown to this comparison", file=sys.stderr)
    if left_labels_path and right_labels_path and str(left_labels_path) != str(right_labels_path):
        print(f"refusing to compare: the runs were scored against different label files "
              f"({left_labels_path} vs {right_labels_path})", file=sys.stderr)
        return 2
    left_labels_sha = (left.get("config") or {}).get("labels_sha256")
    right_labels_sha = (right.get("config") or {}).get("labels_sha256")
    if left_labels_sha and right_labels_sha and left_labels_sha != right_labels_sha:
        # One path, two revisions: the two arms were not scored against the same relevance.
        print(f"refusing to compare: the runs were scored against different revisions of the label "
              f"file ({str(left_labels_sha)[:12]} vs {str(right_labels_sha)[:12]})", file=sys.stderr)
        return 2
    labels: dict[str, set[str]] = {}
    labels_path = left_labels_path or right_labels_path
    recorded_labels_sha = left_labels_sha or right_labels_sha
    labels_pin = "unpinned"
    if labels_path:
        current_labels_sha = file_sha256(pathlib.Path(labels_path))
        if not recorded_labels_sha:
            # A run that predates the hash cannot be checked.  Say so rather than implying the
            # count was reproduced from the revision the run actually used.
            labels_pin = "unpinned"
        elif current_labels_sha == recorded_labels_sha:
            labels_pin = "matched"
        else:
            labels_pin = "stale"
            print(f"warning: the labels at {labels_path} no longer hash to the revision this run "
                  f"recorded (recorded {str(recorded_labels_sha)[:12]}, now "
                  f"{(current_labels_sha or 'unreadable')[:12]}); the reported counts are "
                  f"withheld", file=sys.stderr)
    if COMPARISON_DENOMINATOR != "judged":
        # The declared denominator needs relevance.  Fail closed rather than fall back to the
        # recorded universe: quietly reading a 'confirmable' rule over every judged query is the
        # mismatch this card exists to make visible.
        if not labels_path:
            print(f"refusing to compare: the rule card declares the '{COMPARISON_DENOMINATOR}' "
                  f"denominator and neither run recorded the labels it was scored against",
                  file=sys.stderr)
            return 2
        if labels_pin == "stale":
            print(f"refusing to compare: the rule card declares the '{COMPARISON_DENOMINATOR}' "
                  f"denominator and the labels at {labels_path} are not the revision this run was "
                  f"scored against", file=sys.stderr)
            return 2
        labels = load_labels(pathlib.Path(labels_path))
        if not labels:
            print(f"refusing to compare: the '{COMPARISON_DENOMINATOR}' denominator needs the "
                  f"labels at {labels_path}, and they are empty or unreadable", file=sys.stderr)
            return 2
    left_per = left["metrics"].get("per_query") or {}
    right_per = right["metrics"].get("per_query") or {}
    shared = sorted(set(left_per) & set(right_per))
    if not shared:
        print("no query is judged in both runs; build the labels first (--pool, then judge)",
              file=sys.stderr)
        return 2

    if not labels and labels_path:
        # Reported, not gated: how many of these queries could have differed is part of reading the
        # result, and leaving it out is how a "confirming" gate passed on queries that could not
        # confirm anything.  A label file that has moved leaves the count '?' rather than blocking a
        # 'judged' card, which does not need it.
        labels = load_labels(pathlib.Path(labels_path))

    primary_deltas = {d: right_per[d][PRIMARY_METRIC] - left_per[d][PRIMARY_METRIC] for d in shared}
    counts = {
        "judged": len(shared),
        "confirmable": (
            len(comparison_universe(shared, labels, "confirmable"))
            if labels and labels_pin != "stale" else None
        ),
        "discordant": sum(1 for value in primary_deltas.values() if abs(value) > 1e-9),
        "labels_pin": labels_pin,
    }
    universe = comparison_universe(shared, labels, COMPARISON_DENOMINATOR, primary_deltas)
    if not universe:
        print(f"refusing to compare: the '{COMPARISON_DENOMINATOR}' comparison universe is empty",
              file=sys.stderr)
        return 2

    fields = ("success", "recall", "reciprocal_rank", "ndcg")
    print(f"left : {left_path}\n  {json.dumps({k: v for k, v in left['metrics'].items() if k != 'per_query'}, sort_keys=True)}")
    print(f"right: {right_path}\n  {json.dumps({k: v for k, v in right['metrics'].items() if k != 'per_query'}, sort_keys=True)}")
    print(f"rule : {RULE_VERSION} sha256={RULE_SHA256[:12]} denominator={COMPARISON_DENOMINATOR} "
          f"({rule_provenance()['path']})")
    pin_note = {
        "matched": "labels pinned",
        "unpinned": "labels not pinned by the run",
        "stale": "labels stale at the recorded path",
    }[labels_pin]
    print(f"judged queries: {len(shared)}   comparison universe: {COMPARISON_DENOMINATOR} = "
          f"{len(universe)}   (judged {counts['judged']} / confirmable "
          f"{counts['confirmable'] if counts['confirmable'] is not None else '?'} / discordant "
          f"{counts['discordant']}; {pin_note})")
    print()
    print(f"{'metric':18} {'left':>8} {'right':>8} {'diff':>9} {'wins':>5} {'losses':>6} {'ties':>5} {'sign p':>7} {'bootstrap 95% CI':>22}")
    for field in fields:
        diffs = [right_per[d][field] - left_per[d][field] for d in universe]
        wins = sum(1 for value in diffs if value > 1e-9)
        losses = sum(1 for value in diffs if value < -1e-9)
        ties = len(diffs) - wins - losses
        ci = _bootstrap_ci(diffs)
        mean_left = sum(left_per[d][field] for d in universe) / len(universe)
        mean_right = sum(right_per[d][field] for d in universe) / len(universe)
        ci_text = f"[{ci[0]:+.3f}, {ci[1]:+.3f}] p={ci[2]:.3f}" if ci else "-"
        sign = _sign_test(wins, losses)
        sign_text = f"{sign:.4f}" if sign is not None else "-"
        print(f"{field:18} {mean_left:8.4f} {mean_right:8.4f} {mean_right - mean_left:+9.4f} "
              f"{wins:5d} {losses:6d} {ties:5d} {sign_text:>7} {ci_text:>22}")

    primary_diffs = [right_per[d][PRIMARY_METRIC] - left_per[d][PRIMARY_METRIC] for d in universe]
    secondary_diffs = [
        [right_per[d][field] - left_per[d][field] for d in universe] for field in SECONDARY_METRICS
    ]

    print()
    print(f"pre-registered decision rule ({rule_provenance()['path']} {RULE_VERSION}; history: "
          f"benchmarks/search_eval_decisions.md, benchmarks/search_eval_decisions_round2.md), "
          f"primary={PRIMARY_METRIC}:")
    decision = _decision(primary_diffs, secondary_diffs, counts)
    for line in decision:
        print(line)
    print(f"  => {'RULE MET: flipping the default is warranted' if all('FAIL' not in l for l in decision) else 'RULE NOT MET: keep the default'}")

    print()
    print("per-query MRR deltas (right - left), only the queries that moved:")
    moved = [d for d in shared if abs(right_per[d]["reciprocal_rank"] - left_per[d]["reciprocal_rank"]) > 1e-9]
    for digest in moved:
        row = right["results"].get(digest, {})
        print(f"  {row.get('query', digest)!r}: {left_per[digest]['reciprocal_rank']:.3f} -> "
              f"{right_per[digest]['reciprocal_rank']:.3f}")
    if not moved:
        print("  none")
        # Identical output under different configs is possible but it is also what a harness that
        # pins the environment produces, and a table of 40 ties reads as "no difference" rather than
        # "nothing was compared".  The configs are compared above; this says so out loud.
        print("  WARNING: every judged query is identical under two different configs -- check that the "
              "harness is not forcing one of them", file=sys.stderr)
    return 0


# --- pool building for judging -----------------------------------------------------------------


def build_pool(
    queries: list[dict],
    vectors: dict[str, list[float]],
    top_k: int,
    judged: set[str] | None = None,
) -> None:
    """Print the union of every fusion's top-k, blinded: keys sorted, no scores, no origins.

    ``judged`` skips queries that already have labels, so a second round of queries can be pooled
    without re-reading the first round's candidates.
    """
    if judged:
        queries = [item for item in queries if query_digest(item["query"]) not in judged]
    pooled: dict[str, set[str]] = {query_digest(item["query"]): set() for item in queries}
    for fusion in POOL_FUSIONS:
        for digest, row in run(queries, vectors, top_k, fusion=fusion).items():
            pooled[digest].update(entry["key"] for entry in row["returned"])

    catalog, _note = None, None
    from vector_lake import page_index_projection

    catalog, _note = page_index_projection.read_catalog()
    for item in queries:
        digest = query_digest(item["query"])
        keys = sorted(pooled[digest])
        print(f"### {item['query']}  [{digest}]  ({len(keys)} candidates)")
        for key in keys:
            node = catalog.nodes_by_key([key]).get(key) if catalog else None
            title = (node or {}).get("title") or ""
            summary = " ".join(str((node or {}).get("summary") or "").split())[:230]
            print(f"- {key} | {title} | {summary}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queries", default=str(QUERY_SET))
    parser.add_argument("--labels", default=str(LABEL_SET))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--vectors", choices=("snapshot", "live", "stored"), default="snapshot")
    parser.add_argument("--out", default="")
    parser.add_argument("--pool", action="store_true")
    parser.add_argument("--pool-judged", action="store_true",
                        help="pool queries that already have labels too")
    parser.add_argument("--compare", nargs=2, metavar=("LEFT", "RIGHT"), default=None)
    args = parser.parse_args()

    if args.compare:
        return compare(args.compare[0], args.compare[1], args.top_k)

    queries = load_queries(pathlib.Path(args.queries), args.limit)
    vectors = build_vectors(queries, args.vectors)

    if args.pool:
        # Leading comment so the pool file records the corpus it was drawn from; the pool builders
        # and ``_read_jsonl`` both skip lines starting with ``#``.
        print(f"# corpus {corpus_fingerprint()}")
        judged = set() if args.pool_judged else set(load_labels(pathlib.Path(args.labels)))
        build_pool(queries, vectors, args.top_k, judged)
        return 0

    labels = load_labels(pathlib.Path(args.labels))
    if not labels:
        # Found by using it: a wrong ``--labels`` path silently yielded ``judged: 0``, a run file that
        # looks perfectly valid, and a comparison of two systems over nothing.  Refuse instead.
        print(f"refusing to replay: {args.labels} produced no labels; a run that judges nothing "
              f"cannot tell two systems apart (check the path)", file=sys.stderr)
        return 2
    corpus = corpus_fingerprint()
    judged_against = labels_corpus(pathlib.Path(args.labels))
    if judged_against and judged_against != corpus:
        # Not fatal -- re-scoring an old label set can be the point -- but it must not be silent:
        # this is exactly how the second batch's pool and its scoring runs came apart.
        print(f"[corpus] WARNING: labels were judged against corpus {judged_against}, "
              f"this run reads {corpus}; unjudged pages count as not relevant", file=sys.stderr)
    results = run(queries, vectors, args.top_k)
    payload = {
        "rule": rule_provenance(),
        "config": {
            "fusion": os.environ.get("VECTOR_LAKE_FUSION", "sum"),
            "expansion_quota": os.environ.get("VECTOR_LAKE_EXPANSION_QUOTA", ""),
            # The entity-name tier changes the ordering, so it belongs in the config record for the
            # same reason fusion and the expansion quota do: without it, --compare refuses the pair
            # as "the same configuration" -- which is the honest reading, and it also means two runs
            # with different ranking behaviour would otherwise look identical in the record.
            "entity_name_priority": os.environ.get("VECTOR_LAKE_ENTITY_NAME_PRIORITY", "0"),
            # Same reason: the author facet changes the ordering when it is on, and an unrecorded
            # switch makes two materially different runs look like one configuration.
            "author_facet": os.environ.get("VECTOR_LAKE_AUTHOR_FACET", "off"),
            "author_boost": os.environ.get("VECTOR_LAKE_AUTHOR_BOOST", ""),
            "author_sources": os.environ.get("VECTOR_LAKE_AUTHOR_SOURCES", ""),
            # The source demotion is a ranking preference like the others, and it had never been
            # scored on this label set: an audit of where the judged-relevant pages are lost found
            # 7 of 12 "in the pool, ranked out" pages were Sources, so its 0.6 is a hypothesis.
            "source_rank_penalty": os.environ.get("VECTOR_LAKE_SOURCE_RANK_PENALTY", ""),
            # The lexical backend is a ranking input like the rest: it decides which candidates
            # reach top_k and how the lexical signal scores.  Without it here, an FTS5 run and a
            # tantivy run record the *same* configuration and --compare refuses the pair as
            # "both runs used the same config", which is exactly what happened on 2026-09-25 while
            # evaluating tantivy by hand with the switch set per arm.
            "fts_backend": _fts_backend(),
            "top_k": args.top_k,
            "vectors": args.vectors,
            "labels": str(args.labels),
            "labels_sha256": file_sha256(pathlib.Path(args.labels)),
            "corpus": corpus,
            "labels_corpus": judged_against or "unrecorded",
        },
        "metrics": metrics(results, labels, args.top_k),
        "results": results,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    summary = {k: v for k, v in payload["metrics"].items() if k != "per_query"}
    print(json.dumps(payload["config"], sort_keys=True), file=sys.stderr)
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
