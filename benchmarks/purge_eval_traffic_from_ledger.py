"""Take evaluation traffic out of the production search ledger, once.

Why this is needed even though the harness is fixed: ``benchmarks/search_replay.py`` now sets
``VECTOR_LAKE_SEARCH_LEDGER=0`` for the duration of a replay, but the rows written before that
guard existed are still in the live file.  Measured 2026-09-23: **893 of the 901 entries (99.1%)
were evaluation traffic**, and only 8 were operator queries -- so every readiness reading taken
from the ledger ("341 distinct queries", the latency and origin mixes) was describing the
evaluation sets, not the lake in use.

The ledger stores no query text, by design, so the two cannot be told apart by reading it.  They
can be told apart by digest: the label files hold the evaluation queries, this computes their
``q_hash`` and splits the ledger on that.

  --dry-run (default) reports the split and writes nothing.
  --apply  writes the production rows back to the live file and the evaluation rows to
           ``search_ledger.eval-legacy.jsonl`` beside it, so nothing is discarded.
"""

import argparse
import glob
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from vector_lake.search_ledger import ledger_path, query_digest  # noqa: E402

LABEL_GLOB = "search_eval_labels*.jsonl"


def evaluation_digests() -> tuple[set[str], dict[str, int]]:
    """``(digests, malformed-line counts)`` from every label file on disk."""
    digests: set[str] = set()
    malformed: dict[str, int] = {}
    for name in sorted(glob.glob(str(pathlib.Path(__file__).resolve().parent / LABEL_GLOB))):
        for line in pathlib.Path(name).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # The r2/r3 files carry a header line that is not JSON; that is the instrument's
                # shape, not a fault, so it is counted rather than raised.
                malformed[pathlib.Path(name).name] = malformed.get(pathlib.Path(name).name, 0) + 1
                continue
            query = str(entry.get("query") or "").strip()
            if query:
                digests.add(query_digest(query))
    return digests, malformed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    eval_digests, malformed = evaluation_digests()
    path = pathlib.Path(ledger_path())
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    production = [row for row in rows if row.get("q_hash") not in eval_digests]
    evaluation = [row for row in rows if row.get("q_hash") in eval_digests]

    print(f"label files: {len(glob.glob(str(pathlib.Path(__file__).resolve().parent / LABEL_GLOB)))}"
          f" | evaluation queries: {len(eval_digests)} | non-JSON header lines: {malformed}")
    print(f"ledger {path.name}: {len(rows)} entries = evaluation {len(evaluation)} + production {len(production)}")
    print(f"  distinct digests: evaluation {len({r['q_hash'] for r in evaluation})}, "
          f"production {len({r['q_hash'] for r in production})}")

    if not args.apply:
        print("dry run: nothing written")
        return 0

    sidecar = path.with_name("search_ledger.eval-legacy.jsonl")
    with open(sidecar, "w", encoding="utf-8") as handle:
        for row in evaluation:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(path, "w", encoding="utf-8") as handle:
        for row in production:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(production)} production entr(ies) to {path.name}")
    print(f"moved {len(evaluation)} evaluation entr(ies) to {sidecar.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
