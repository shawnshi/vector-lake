"""Criterion satisfiability: can the corpus answer the queries the pool missed at all?

# Pre-registration (2026-09-23, before the first run)

**Question.** Batch 2 measured 80% of queries with no relevant page in the top-5 pool, and P0 found
79.5% with none in the top-20 either.  The plan records that this "is not a corpus problem (vectors
are at 100% coverage)" but also that the earlier probe was loose on mixed Chinese/English text and
"can only be indicative".  The open question is therefore not "did ranking fail" but "is there an
answer in the corpus for ranking to find".

**Instrument.** For each query, look for a page that *names* the query:

* tokens: whitespace-separated, kept when >= 2 characters for CJK runs and >= 3 for Latin;
* ``phrase`` -- the whole query, normalized (lowercased, whitespace removed), contained in the
  page's title or one of its aliases;
* ``tokens`` -- every token contained in the page's title + aliases.

**Denominator.** The batch-3 query set (``search_eval_labels_r3_consensus.jsonl``), and inside it
the subset the labels record as having a pool with no relevant page.

**What this instrument cannot say, declared up front.** It measures *naming*, not substantiation:
a page whose title contains the query may still not answer it, which is exactly the loose probe the
plan rejected.  So its result is read as an upper bound on satisfiability, and a negative result
(``not_found``) is the load-bearing one: no page even names it, so no ranking could have surfaced
one.

**What a result would mean.** A large ``not_found`` share among zero-recall queries says the
criterion or the corpus is the constraint and ranking work cannot move it -- which is the plan's
"P0.5 判据可满足性" question, answered without a new judging round.  A small one says the answers
exist and the ranking/pool is what loses them.

    python benchmarks/criterion_satisfiability.py            # report
    python benchmarks/criterion_satisfiability.py --json     # machine-readable
"""

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from vector_lake.node_vocabulary import NON_NODE_WIKI_FILES, is_generated_artifact  # noqa: E402
from vector_lake.wiki_utils import get_wiki_dir, read_markdown_file  # noqa: E402

LABELS = pathlib.Path(__file__).resolve().parent / "search_eval_labels_r3_consensus.jsonl"
TOKEN_STRIP = re.compile(r"[\s\u3000,，。、:：;；!！?？\"'“”‘’()（）\[\]【】<>《》/\\|~`^*_+=#-]+")


def normalize(text: str) -> str:
    return TOKEN_STRIP.sub("", str(text).lower())


def tokens_of(query: str) -> list[str]:
    tokens = []
    for token in TOKEN_STRIP.split(str(query).lower()):
        if not token:
            continue
        if re.search(r"[\u4e00-\u9fff]", token):
            if len(token) >= 2:
                tokens.append(token)
        elif len(token) >= 3:
            tokens.append(token)
    return tokens


def load_pages() -> list[dict]:
    wiki = get_wiki_dir()
    pages = []
    for path in sorted(wiki.glob("*.md")):
        if path.name in NON_NODE_WIKI_FILES:
            continue
        frontmatter, body, _ = read_markdown_file(path)
        if is_generated_artifact(frontmatter, path.name, body):
            continue
        names = [str(frontmatter.get("title") or ""), *[str(a) for a in frontmatter.get("aliases") or []]]
        pages.append({"key": path.name[:-3], "names": [n for n in names if n]})
    return pages


def load_queries() -> list[dict]:
    queries = []
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # the header line
        query = str(entry.get("query") or "").strip()
        if query:
            queries.append({"query": query, "relevant": [str(k) for k in entry.get("relevant") or []]})
    return queries


def classify(query: str, pages: list[dict], limit: int = 3) -> dict:
    needle = normalize(query)
    tokens = tokens_of(query)
    phrase_hits, token_hits = [], []
    for page in pages:
        normalized = [normalize(name) for name in page["names"]]
        if needle and any(needle in name for name in normalized):
            phrase_hits.append(page["key"])
        elif tokens and all(any(token in name for name in normalized) for token in tokens):
            token_hits.append(page["key"])
    verdict = "phrase" if phrase_hits else ("tokens" if token_hits else "not_found")
    return {
        "verdict": verdict,
        "pages": (phrase_hits or token_hits)[:limit],
        "hit_count": len(phrase_hits) + len(token_hits),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    pages = load_pages()
    queries = load_queries()
    rows = []
    for item in queries:
        outcome = classify(item["query"], pages)
        rows.append({**item, **outcome, "pool_had_relevant": bool(item["relevant"])})

    zero_recall = [row for row in rows if not row["pool_had_relevant"]]
    verdicts = collections.Counter(row["verdict"] for row in rows)
    zero_verdicts = collections.Counter(row["verdict"] for row in zero_recall)

    def share(counter: collections.Counter, total: int, key: str) -> str:
        return f"{counter.get(key, 0)}/{total} ({counter.get(key, 0) * 100.0 / max(total, 1):.1f}%)"

    report = {
        "pages_searched": len(pages),
        "queries": len(rows),
        "verdicts": dict(verdicts),
        "zero_recall_queries": len(zero_recall),
        "zero_recall_verdicts": dict(zero_verdicts),
        "zero_recall_not_found_share": round(
            zero_verdicts.get("not_found", 0) / max(len(zero_recall), 1), 4
        ),
        "examples": {
            "naming_but_no_relevant_label": [
                {"query": row["query"], "pages": row["pages"], "verdict": row["verdict"]}
                for row in zero_recall
                if row["verdict"] != "not_found"
            ][:5],
            "nothing_names_them": [
                {"query": row["query"]} for row in zero_recall if row["verdict"] == "not_found"
            ][:5],
        },
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print(f"pages searched: {len(pages)}  (generated artifacts and non-nodes excluded)")
    print(f"queries:        {len(rows)}")
    print(f"  phrase   {share(verdicts, len(rows), 'phrase')}")
    print(f"  tokens   {share(verdicts, len(rows), 'tokens')}")
    print(f"  none     {share(verdicts, len(rows), 'not_found')}")
    print()
    print(f"zero-relevance pool (the batch-2/3 symptom): {len(zero_recall)}")
    print(f"  named by a page   {share(zero_verdicts, len(zero_recall), 'phrase')} through 'tokens' too: "
          f"{zero_verdicts.get('tokens', 0)}")
    print(f"  named by nothing  {share(zero_verdicts, len(zero_recall), 'not_found')}"
          "   <- the load-bearing number")
    for row in report["examples"]["naming_but_no_relevant_label"][:3]:
        print(f"    e.g. '{row['query'][:40]}' -> {row['pages'][:2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
