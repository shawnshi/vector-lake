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
                          ("vec_embeddings", "entity_id"),
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
    row = conn.execute("select embedding from vec_embeddings order by entity_id limit 1").fetchone()
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


#: The pre-registered decision rule, from ``benchmarks/search_eval_decisions.md`` -- that file is the
#: authority and this is its implementation.  Registered before the confirming queries existed, so
#: the primary metric cannot be chosen after seeing which one passed.
#:
#: Two of the registered conditions were recorded in the registrations and never implemented here:
#:
#: * the +0.05 minimum effect (``search_eval_decisions.md`` §修正点, then the second batch's §3);
#:   without it the harness passes a difference of +0.0132 that the registration calls a failure;
#: * the sign test's demotion to a reported number.  The first batch showed the sign test only reads
#:   signs, needs ~780 discordant pairs at the observed win rate and is therefore almost impossible
#:   to pass even when the effect is real, so the second batch made the bootstrap interval the only
#:   significance gate.  Requiring it here would fail the run on the wrong condition -- and would
#:   report a failure on a gate the registration no longer has.
#:
#: PRIMARY_METRIC is nDCG@5 because it penalises both ways the two fusions trade off (a query's own
#: page not ranking first, and later relevant pages being pushed out), while MRR sees only the first.
PRIMARY_METRIC = "ndcg"
SECONDARY_METRICS = ("success", "recall", "reciprocal_rank")
MIN_CONFIRMING_QUERIES = 300
MIN_PRIMARY_EFFECT = 0.05
PRIMARY_SIGN_ALPHA = 0.05


def _decision(diffs: list[float], secondary: list[list[float]]) -> list[str]:
    """Evaluate the registered rule.  Returns one line per condition, pass or fail."""
    wins = sum(1 for value in diffs if value > 1e-9)
    losses = sum(1 for value in diffs if value < -1e-9)
    mean = sum(diffs) / len(diffs) if diffs else 0.0
    ci = _bootstrap_ci(diffs)
    sign = _sign_test(wins, losses) if (wins + losses) else None
    lines = []
    lines.append(f"  {'PASS' if mean >= MIN_PRIMARY_EFFECT else 'FAIL'}  "
                 f"primary mean difference >= registered minimum effect  "
                 f"({mean:+.4f} vs {MIN_PRIMARY_EFFECT:+.2f})")
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
    lines.append(f"  {'PASS' if len(diffs) >= MIN_CONFIRMING_QUERIES else 'FAIL'}  "
                 f"at least {MIN_CONFIRMING_QUERIES} confirming queries  ({len(diffs)})")
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
    left_per = left["metrics"].get("per_query") or {}
    right_per = right["metrics"].get("per_query") or {}
    shared = sorted(set(left_per) & set(right_per))
    if not shared:
        print("no query is judged in both runs; build the labels first (--pool, then judge)",
              file=sys.stderr)
        return 2

    fields = ("success", "recall", "reciprocal_rank", "ndcg")
    print(f"left : {left_path}\n  {json.dumps({k: v for k, v in left['metrics'].items() if k != 'per_query'}, sort_keys=True)}")
    print(f"right: {right_path}\n  {json.dumps({k: v for k, v in right['metrics'].items() if k != 'per_query'}, sort_keys=True)}")
    print(f"judged queries: {len(shared)}")
    print()
    print(f"{'metric':18} {'left':>8} {'right':>8} {'diff':>9} {'wins':>5} {'losses':>6} {'ties':>5} {'sign p':>7} {'bootstrap 95% CI':>22}")
    for field in fields:
        diffs = [right_per[d][field] - left_per[d][field] for d in shared]
        wins = sum(1 for value in diffs if value > 1e-9)
        losses = sum(1 for value in diffs if value < -1e-9)
        ties = len(diffs) - wins - losses
        ci = _bootstrap_ci(diffs)
        mean_left = sum(left_per[d][field] for d in shared) / len(shared)
        mean_right = sum(right_per[d][field] for d in shared) / len(shared)
        ci_text = f"[{ci[0]:+.3f}, {ci[1]:+.3f}] p={ci[2]:.3f}" if ci else "-"
        sign = _sign_test(wins, losses)
        sign_text = f"{sign:.4f}" if sign is not None else "-"
        print(f"{field:18} {mean_left:8.4f} {mean_right:8.4f} {mean_right - mean_left:+9.4f} "
              f"{wins:5d} {losses:6d} {ties:5d} {sign_text:>7} {ci_text:>22}")

    primary_diffs = [right_per[d][PRIMARY_METRIC] - left_per[d][PRIMARY_METRIC] for d in shared]
    secondary_diffs = [
        [right_per[d][field] - left_per[d][field] for d in shared] for field in SECONDARY_METRICS
    ]

    print()
    print(f"pre-registered decision rule (benchmarks/search_eval_decisions.md,\n"
          f"  benchmarks/search_eval_decisions_round2.md), primary={PRIMARY_METRIC}:")
    decision = _decision(primary_diffs, secondary_diffs)
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

    # Evaluation runs must not be mixed into the production ledger, where they would look like
    # operator queries and inflate the distinct-query count.
    os.environ.setdefault("VECTOR_LAKE_SEARCH_LEDGER", "0")

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
        "config": {
            "fusion": os.environ.get("VECTOR_LAKE_FUSION", "sum"),
            "expansion_quota": os.environ.get("VECTOR_LAKE_EXPANSION_QUOTA", ""),
            "top_k": args.top_k,
            "vectors": args.vectors,
            "labels": str(args.labels),
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
