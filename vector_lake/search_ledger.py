"""What search answered, recorded so a ranking change can be measured against a baseline.

The A1/A2 work changes which candidates reach ``top_k``, and the lake keeps no record that could
show whether it improved: there is no ``log.md`` and no query table.  This writes one JSON line per
retrieval -- enough to compare two runs that must agree, and to see which of FTS, the vector
projection or graph expansion actually produced each page.

The query text is **not** stored.  It is caller content, it is not this module's to retain, and a
digest answers the two questions the ledger exists for: "is this the same query as last time" and
"is the same query answering differently than it did".  ``query_chars`` is kept because a digest
alone cannot distinguish a short query from a long one when reading the file by eye.

Bounded and fail-open.  The ledger is a measurement: it must never make a search fail, and it must
not grow without limit.  One generation is rotated aside at ``MAX_BYTES``, so the ceiling is 2x.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import get_runtime_tmp_dir

log = logging.getLogger("vector-lake-search-ledger")

LEDGER_FILENAME = "search_ledger.jsonl"
#: Rotate one generation aside once the live file passes this.  2 MiB holds roughly 4 000 entries
#: at the observed line size, and the ceiling is twice that.
MAX_BYTES = 2 * 1024 * 1024


def ledger_path() -> Path:
    return get_runtime_tmp_dir() / LEDGER_FILENAME


def enabled() -> bool:
    """On by default; ``VECTOR_LAKE_SEARCH_LEDGER=0`` turns it off."""
    return os.environ.get("VECTOR_LAKE_SEARCH_LEDGER", "1").strip() != "0"


def query_digest(query: str) -> str:
    return hashlib.sha256(str(query).encode("utf-8")).hexdigest()[:16]


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        # A rotation failure is not allowed to cost the entry we are about to write; the file
        # simply grows past the ceiling until the next attempt.
        pass


def record(
    query: str,
    *,
    mode: str,
    top_k: int,
    returned: list[dict],
    notes: list[str] | None = None,
    elapsed_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Append one entry.  Never raises: a measurement must not break the thing it measures."""
    if not enabled():
        return
    try:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": str(mode),
            "top_k": int(top_k),
            "q_hash": query_digest(query),
            "q_chars": len(str(query)),
            "returned": returned,
        }
        if notes:
            entry["notes"] = [str(note) for note in notes]
        if elapsed_ms is not None:
            entry["ms"] = round(float(elapsed_ms), 1)
        if error:
            entry["error"] = str(error)

        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.debug("search ledger write skipped: %s: %s", type(exc).__name__, exc)


def entries(limit: int = 0) -> list[dict]:
    """The most recent entries, oldest first.  Read-only; used by ``doctor`` and the harness."""
    path = ledger_path()
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def summary() -> dict:
    """Entry count and distinct-query count, for a doctor line."""
    rows = entries()
    return {
        "entries": len(rows),
        "distinct_queries": len({row.get("q_hash") for row in rows}),
        "path": str(ledger_path()),
    }
