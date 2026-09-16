"""Exact n-gram inverted index over ``operational_memory_index``.

Why this exists
---------------

``search_operational_memory`` scores a memory by counting how many query terms
occur in its key / text / page fields, weighted 4 / 3 / 1.  ``_query_terms``
expands a CJK query into *every single character* plus *every adjacent bigram*,
so a 30-character question becomes ~59 terms and the plain SQL form of the
scorer needs ``O(rows x terms)`` ``instr()`` calls: measured 2.3 s on the live
142 117-row corpus, growing linearly forever.

An inverted index turns that into ``O(sum of the postings of the query terms)``.
Measured on the same corpus, the longest realistic query touches 223 253
postings and accumulates in 0.12 s *in pure Python* -- no numpy, no compression
dependency.

Layout
------

``operational_memory_gram(gram, postings)``
    The base index.  ``postings`` is a little-endian ``uint32`` array where each
    element is ``(doc_delta << 4) | field_mask``; ``doc_delta`` is the gap to the
    previous document so the list stays sorted and compact, and ``field_mask``
    has bit 0 = key, bit 1 = text, bit 2 = page.  Measured 414 914 distinct grams
    and 25 430 371 postings = 97 MB, against 162 MB for the dead FTS projection
    this replaced.  A posting list averages 61 entries, so decoding is cheap.

``operational_memory_gram_overlay(gram, doc, mask)``
    Recent writes.  A plain B-tree table so applying one document is
    ``DELETE ... WHERE doc = ?`` plus an ``executemany`` insert -- O(1) SQL
    statements instead of a read-modify-write of every affected posting blob.
    The query merges base + overlay, with overlay entries winning.

``operational_memory_gram_dirty(doc)``
    Documents whose base postings cannot be trusted.  A document that is dirty is
    authoritative in the overlay, so the query *skips its base entries*.  That is
    what makes the merge exact without storing a document's previous state, and
    it also covers deletions: a deleted document has no projection row, so it
    keeps no overlay entries either, and skipping removes it from the result.

Correctness
-----------

The index reproduces :func:`governance_store._memory_relevance` exactly:
``relevance = sum over terms of 4*[term in key] + 3*[term in text] + [term in page]``.
Terms of one or two characters are looked up directly.  Longer terms are
resolved by intersecting the postings of their constituent bigrams (a necessary
condition) and then verifying each candidate with ``instr``, so the result is
exact too.  ``tests/test_memory_gram_index.py`` asserts equality with the
``legacy`` full-scan oracle.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time

from vector_lake.db_store import get_connection, init_db, transaction

log = logging.getLogger("vector-lake-memory-gram-index")

GRAM_FORMAT_VERSION = 1
#: Below this many stale documents the index self-heals on the read path; above it
#: the caller keeps using the exact scan until an explicit rebuild runs.  A bulk
#: rebuild costs roughly 3 ms/document (431 s for the live 142 117-document
#: corpus), so the automatic path is capped well below that.
AUTO_REBUILD_MAX_DOCS = 2_000
#: Documents materialised into the overlay per call.
DEFAULT_FLUSH_DOCS = 400
#: Overlay grams merged into the base per read-path self-heal call.
COMPACT_GRAMS_PER_CALL = 200
#: Grams merged per chunk by :func:`compact_memory_gram_overlay` (bounded memory).
COMPACT_CHUNK_GRAMS = 5000
#: Ceiling for one explicit compaction call, so a full drain is not unbounded.
COMPACT_MAX_GRAMS = 200_000

_LONG_RUN = re.compile(r"[0-9a-z]{3,}")

# Field-mask weights, indexed by mask (bit0 key, bit1 text, bit2 page).
_FIELD_WEIGHTS = [0] * 16
for _mask in range(8):
    _FIELD_WEIGHTS[_mask] = 4 * (_mask & 1) + 3 * ((_mask >> 1) & 1) + ((_mask >> 2) & 1)


def extract_grams(key_blob: str, text_blob: str, page_blob: str) -> dict[str, int]:
    """Distinct grams of one document, mapped to the fields that contain them.

    Grams are the distinct single characters, the distinct adjacent bigrams and
    the distinct alphanumeric runs of length >= 3.  Characters and bigrams are
    what ``_query_terms`` produces; the runs are indexed so that a long Latin
    token also narrows to a single posting lookup.
    """
    grams: dict[str, int] = {}
    for bit, blob in ((1, key_blob), (2, text_blob), (4, page_blob)):
        if not blob:
            continue
        seen: list[str] = list(blob)
        seen.extend(blob[i : i + 2] for i in range(len(blob) - 1))
        seen.extend(_LONG_RUN.findall(blob))
        for gram in seen:
            grams[gram] = grams.get(gram, 0) | bit
    return grams


def _pack(postings) -> bytes:
    """``[(doc, mask), ...]`` in ascending doc order -> packed little-endian blob."""
    from array import array

    values = array("I")
    previous = 0
    for doc, mask in postings:
        values.append(((doc - previous) << 4) | (mask & 0x0F))
        previous = doc
    return values.tobytes()


def _entries(blob: bytes):
    """Packed blob -> ``[(doc, mask), ...]``."""
    from array import array

    values = array("I")
    values.frombytes(blob)
    if sys.byteorder != "little" and values.itemsize > 1:
        values.byteswap()
    doc = 0
    out = []
    for value in values:
        doc += value >> 4
        out.append((doc, value & 0x0F))
    return out


def _accumulate(blob: bytes, weights: list[int], accumulator: dict, skip=None) -> None:
    """Fold one posting list into ``accumulator`` (``doc -> relevance``)."""
    from array import array

    values = array("I")
    values.frombytes(blob)
    if sys.byteorder != "little" and values.itemsize > 1:
        values.byteswap()
    doc = 0
    if skip:
        for value in values:
            doc += value >> 4
            if doc in skip:
                continue
            mask = value & 0x0F
            if mask:
                accumulator[doc] = accumulator.get(doc, 0) + weights[mask]
    else:
        for value in values:
            doc += value >> 4
            mask = value & 0x0F
            if mask:
                accumulator[doc] = accumulator.get(doc, 0) + weights[mask]


# --- state ------------------------------------------------------------------


def _projection_doc_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0]


def gram_index_state() -> dict:
    conn = get_connection()
    row = conn.execute(
        "SELECT format_version, doc_count, gram_count, postings_count, base_docs, updated_at "
        "FROM operational_memory_gram_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return {"ready": False, "format_version": None, "doc_count": 0, "gram_count": 0,
                "postings_count": 0, "base_docs": 0, "updated_at": None, "base_present": False}
    state = dict(row)
    # The state row is a cache; confirm the base table actually holds something so
    # a truncated base degrades to the exact scan instead of answering "no hits".
    state["base_present"] = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM operational_memory_gram LIMIT 1)"
    ).fetchone()[0] == 1
    state["ready"] = (
        int(state["format_version"] or 0) == GRAM_FORMAT_VERSION
        and int(state["gram_count"] or 0) > 0
        and bool(state["base_present"])
    )
    return state


def pending_doc_count(conn=None) -> int:
    conn = conn or get_connection()
    return conn.execute("SELECT COUNT(*) FROM operational_memory_gram_dirty").fetchone()[0]


def live_dirty_doc_count(conn=None) -> int:
    """Dirty documents that still exist; these are the ones awaiting materialisation."""
    conn = conn or get_connection()
    return conn.execute(
        "SELECT COUNT(*) FROM operational_memory_gram_dirty AS d "
        "WHERE EXISTS (SELECT 1 FROM operational_memory_index AS i WHERE i.rowid = d.doc)"
    ).fetchone()[0]


def retired_doc_count(conn=None) -> int:
    """Dirty markers for deleted documents, whose base postings must keep being skipped."""
    conn = conn or get_connection()
    return pending_doc_count(conn) - live_dirty_doc_count(conn)


def skip_doc_set(conn=None) -> set[int]:
    """Documents whose base postings are stale (edited since the base, or deleted)."""
    conn = conn or get_connection()
    return {int(row[0]) for row in conn.execute("SELECT doc FROM operational_memory_gram_dirty")}


def overlay_row_count(conn=None) -> int:
    conn = conn or get_connection()
    return conn.execute("SELECT COUNT(*) FROM operational_memory_gram_overlay").fetchone()[0]


def _record_state(conn, doc_count: int, gram_count: int, postings_count: int, base_docs: int) -> None:
    conn.execute(
        "INSERT INTO operational_memory_gram_state "
        "(singleton, format_version, doc_count, gram_count, postings_count, base_docs, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "format_version = excluded.format_version, doc_count = excluded.doc_count, "
        "gram_count = excluded.gram_count, postings_count = excluded.postings_count, "
        "base_docs = excluded.base_docs, updated_at = excluded.updated_at",
        (
            GRAM_FORMAT_VERSION, doc_count, gram_count, postings_count, base_docs,
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        ),
    )


# --- maintenance ------------------------------------------------------------


def rebuild_memory_gram_index(dry_run: bool = False, batch_docs: int = 2000) -> str:
    """Rebuild the base index from ``operational_memory_index``.

    Staging happens in a temporary ``(gram, doc, mask)`` table so SQLite performs
    the external sort; holding 25 M postings in a Python dict would need gigabytes.
    """
    conn = get_connection()
    init_db()
    if dry_run:
        state = gram_index_state()
        return (
            f"[DRY RUN] Would rebuild the memory gram index from "
            f"{_projection_doc_count(conn)} document(s); the current base covers "
            f"{int(state['base_docs'] or 0)} document(s) with "
            f"{int(state['gram_count'] or 0)} gram(s) and "
            f"{pending_doc_count(conn)} pending change(s)."
        )

    conn.execute("DROP TABLE IF EXISTS operational_memory_gram_stage")
    conn.execute(
        "CREATE TABLE operational_memory_gram_stage ("
        "gram TEXT NOT NULL, doc INTEGER NOT NULL, mask INTEGER NOT NULL, "
        "PRIMARY KEY (gram, doc)) WITHOUT ROWID"
    )
    # The staging DDL must run before this cursor is opened: a half-consumed
    # read statement on the same connection blocks the DDL.
    docs = conn.execute(
        "SELECT rowid, key_blob, text_blob, page_blob FROM operational_memory_index ORDER BY rowid"
    )
    staged_docs = 0
    while True:
        rows = docs.fetchmany(batch_docs)
        if not rows:
            break
        payload = []
        for row in rows:
            doc = row["rowid"]
            grams = extract_grams(row["key_blob"], row["text_blob"], row["page_blob"])
            payload.extend((gram, doc, mask) for gram, mask in grams.items())
        with transaction():
            conn.executemany(
                "INSERT OR REPLACE INTO operational_memory_gram_stage (gram, doc, mask) VALUES (?, ?, ?)",
                payload,
            )
        staged_docs += len(rows)

    gram_count = postings_count = 0
    with transaction():
        conn.execute("DELETE FROM operational_memory_gram_overlay")
        conn.execute("DELETE FROM operational_memory_gram_dirty")
        conn.execute("DELETE FROM operational_memory_gram")
        cursor = conn.execute(
            "SELECT gram, doc, mask FROM operational_memory_gram_stage ORDER BY gram, doc"
        )
        buffer: list[tuple[int, int]] = []
        current_gram: str | None = None
        while True:
            rows = cursor.fetchmany(50_000)
            if not rows:
                break
            for gram, doc, mask in rows:
                if gram != current_gram:
                    if current_gram is not None:
                        conn.execute(
                            "INSERT INTO operational_memory_gram (gram, postings) VALUES (?, ?)",
                            (current_gram, _pack(buffer)),
                        )
                        gram_count += 1
                        postings_count += len(buffer)
                    current_gram = gram
                    buffer = []
                buffer.append((doc, mask))
        if current_gram is not None:
            conn.execute(
                "INSERT INTO operational_memory_gram (gram, postings) VALUES (?, ?)",
                (current_gram, _pack(buffer)),
            )
            gram_count += 1
            postings_count += len(buffer)
        _record_state(conn, staged_docs, gram_count, postings_count, staged_docs)
    conn.execute("DROP TABLE IF EXISTS operational_memory_gram_stage")
    return (
        f"Rebuilt the memory gram index: {gram_count} gram(s), {postings_count} posting(s) "
        f"over {staged_docs} document(s)."
    )


def flush_memory_gram_dirty(limit_docs: int = DEFAULT_FLUSH_DOCS) -> dict:
    """Apply queued document changes to the overlay.

    Every dirty document that still exists is removed from the overlay and
    re-inserted from its current projection row.  Documents that no longer exist
    stay dirty on purpose: the query skips base postings for dirty documents, so
    staying dirty is what retires a deleted document's postings.
    """
    conn = get_connection()
    docs = [row[0] for row in conn.execute(
        "SELECT doc FROM operational_memory_gram_dirty LIMIT ?", (int(limit_docs),)
    )]
    if not docs:
        return {"flushed": 0, "overlay_rows": 0, "retired": 0, "remaining": 0}
    placeholders = ",".join("?" for _ in docs)
    inserted = 0
    with transaction():
        conn.executemany(
            "DELETE FROM operational_memory_gram_overlay WHERE doc = ?", [(doc,) for doc in docs]
        )
        rows = conn.execute(
            f"SELECT rowid, key_blob, text_blob, page_blob FROM operational_memory_index "
            f"WHERE rowid IN ({placeholders})",
            docs,
        ).fetchall()
        payload = []
        for row in rows:
            doc = row["rowid"]
            for gram, mask in extract_grams(row["key_blob"], row["text_blob"], row["page_blob"]).items():
                payload.append((gram, doc, mask))
        if payload:
            conn.executemany(
                "INSERT OR REPLACE INTO operational_memory_gram_overlay (gram, doc, mask) VALUES (?, ?, ?)",
                payload,
            )
            inserted = len(payload)
        # Only documents that still exist leave the queue; deleted ones stay dirty
        # so their base postings keep being skipped.
        live = [int(row["rowid"]) for row in rows]
        if live:
            conn.executemany(
                "DELETE FROM operational_memory_gram_dirty WHERE doc = ?", [(doc,) for doc in live]
            )
        state = conn.execute(
            "SELECT base_docs FROM operational_memory_gram_state WHERE singleton = 1"
        ).fetchone()
        _record_state(
            conn,
            _projection_doc_count(conn),
            conn.execute("SELECT COUNT(*) FROM operational_memory_gram").fetchone()[0],
            int(state["base_docs"] or 0) if state else 0,
            int(state["base_docs"] or 0) if state else 0,
        )
    return {
        "flushed": len(live),
        "overlay_rows": inserted,
        "retired": len(docs) - len(live),
        "remaining": pending_doc_count(conn),
    }


def compact_memory_gram_overlay(limit_grams: int = COMPACT_GRAMS_PER_CALL) -> dict:
    """Merge overlay rows into the base postings and clear the merged part.

    Read-modify-write per affected gram, so the read path's self-heal calls it
    with the small default while an operator drain passes ``None`` to merge
    everything (in bounded chunks).  Safe to interrupt: the overlay rows are
    deleted in the same transaction that rewrites the base blobs.
    """
    conn = get_connection()
    available = conn.execute(
        "SELECT COUNT(DISTINCT gram) FROM operational_memory_gram_overlay"
    ).fetchone()[0]
    if not available:
        return {"grams": 0, "postings_before": 0, "postings_after": 0}
    budget = int(available if limit_grams is None else limit_grams)
    budget = min(budget, COMPACT_MAX_GRAMS, int(available))

    merged = 0
    for start in range(0, budget, COMPACT_CHUNK_GRAMS):
        merged += _compact_gram_chunk(min(COMPACT_CHUNK_GRAMS, budget - start))
    return {
        "grams": merged,
        "remaining_grams": conn.execute(
            "SELECT COUNT(DISTINCT gram) FROM operational_memory_gram_overlay"
        ).fetchone()[0],
    }


def _compact_gram_chunk(limit_grams: int) -> int:
    conn = get_connection()
    grams = [row[0] for row in conn.execute(
        "SELECT DISTINCT gram FROM operational_memory_gram_overlay LIMIT ?", (int(limit_grams),)
    )]
    if not grams:
        return 0
    merged = 0
    with transaction():
        placeholders = ",".join("?" for _ in grams)
        overlay: dict[str, list[tuple[int, int]]] = {gram: [] for gram in grams}
        for gram, doc, mask in conn.execute(
            f"SELECT gram, doc, mask FROM operational_memory_gram_overlay "
            f"WHERE gram IN ({placeholders}) ORDER BY gram, doc",
            grams,
        ):
            overlay[gram].append((int(doc), int(mask)))
        base_rows = {
            row[0]: row[1]
            for row in conn.execute(
                f"SELECT gram, postings FROM operational_memory_gram WHERE gram IN ({placeholders})",
                grams,
            )
        }
        for gram in grams:
            entries = dict(_entries(base_rows[gram])) if gram in base_rows else {}
            for doc, mask in overlay[gram]:
                entries[doc] = mask
            conn.execute(
                "INSERT INTO operational_memory_gram (gram, postings) VALUES (?, ?) "
                "ON CONFLICT(gram) DO UPDATE SET postings = excluded.postings",
                (gram, _pack(sorted(entries.items()))),
            )
            merged += 1
        conn.execute(
            f"DELETE FROM operational_memory_gram_overlay WHERE gram IN ({placeholders})", grams
        )
        state = conn.execute(
            "SELECT base_docs FROM operational_memory_gram_state WHERE singleton = 1"
        ).fetchone()
        _record_state(
            conn,
            _projection_doc_count(conn),
            conn.execute("SELECT COUNT(*) FROM operational_memory_gram").fetchone()[0],
            int(state["base_docs"] or 0) if state else 0,
            int(state["base_docs"] or 0) if state else 0,
        )
    return merged


def prune_retired_gram_docs() -> dict:
    """Drop deleted documents' postings from the base and clear their markers.

    A retired document's own grams are no longer known, so this is a full scan of
    the base posting blobs.  It is a maintenance operation (seconds), not a read
    path step; until it runs, the retired documents are simply skipped.
    """
    conn = get_connection()
    retired = {
        int(row[0])
        for row in conn.execute(
            "SELECT doc FROM operational_memory_gram_dirty AS d "
            "WHERE NOT EXISTS (SELECT 1 FROM operational_memory_index AS i WHERE i.rowid = d.doc)"
        )
    }
    if not retired:
        return {"retired": 0, "grams_rewritten": 0}
    rewritten = 0
    with transaction():
        cursor = conn.execute("SELECT gram, postings FROM operational_memory_gram")
        while True:
            rows = cursor.fetchmany(5000)
            if not rows:
                break
            updates = []
            for gram, blob in rows:
                entries = _entries(blob)
                kept = [entry for entry in entries if entry[0] not in retired]
                if len(kept) != len(entries):
                    updates.append((gram, _pack(kept)))
            if updates:
                conn.executemany(
                    "UPDATE operational_memory_gram SET postings = ? WHERE gram = ?",
                    [(blob, gram) for gram, blob in updates],
                )
                rewritten += len(updates)
        conn.executemany(
            "DELETE FROM operational_memory_gram_dirty WHERE doc = ?", [(doc,) for doc in retired]
        )
        state = conn.execute(
            "SELECT base_docs FROM operational_memory_gram_state WHERE singleton = 1"
        ).fetchone()
        _record_state(
            conn,
            _projection_doc_count(conn),
            conn.execute("SELECT COUNT(*) FROM operational_memory_gram").fetchone()[0],
            int(state["base_docs"] or 0) if state else 0,
            int(state["base_docs"] or 0) if state else 0,
        )
    return {"retired": len(retired), "grams_rewritten": rewritten}


# --- readiness --------------------------------------------------------------


def gram_index_usable() -> bool:
    """True when the index can answer exactly right now."""
    try:
        if not gram_index_state()["ready"]:
            return False
        # Only documents that still exist need materialising; retired markers are
        # removed by :func:`prune_retired_gram_docs`, not by the read path.
        return live_dirty_doc_count() <= AUTO_REBUILD_MAX_DOCS
    except Exception:  # noqa: BLE001 - a missing table simply means "not usable"
        return False


def ensure_memory_gram_index() -> bool:
    """Drain a bounded backlog and report whether the index is usable.

    A stale-but-small backlog is drained incrementally; a large one needs an
    explicit :func:`rebuild_memory_gram_index`, so the caller keeps using the
    exact scan meanwhile rather than paying minutes inside a read.
    """
    conn = get_connection()
    try:
        init_db()
        state = gram_index_state()
        if not state["ready"]:
            if _projection_doc_count(conn) <= AUTO_REBUILD_MAX_DOCS:
                rebuild_memory_gram_index()
            return gram_index_usable()
        if pending_doc_count(conn):
            flush_memory_gram_dirty()
            if live_dirty_doc_count(conn) > AUTO_REBUILD_MAX_DOCS:
                return False
        if overlay_row_count(conn):
            compact_memory_gram_overlay()
        return gram_index_usable()
    except Exception as exc:  # noqa: BLE001 - any fault must degrade, never raise
        log.warning("Memory gram index unusable (%s: %s); falling back to the exact scan.", type(exc).__name__, exc)
        return False


# --- query ------------------------------------------------------------------


def _term_base(gram: str, conn) -> bytes | None:
    row = conn.execute(
        "SELECT postings FROM operational_memory_gram WHERE gram = ?", (gram,)
    ).fetchone()
    return row[0] if row else None


def _term_overlay(gram: str, conn) -> list[tuple[int, int]]:
    return [
        (int(row[0]), int(row[1]))
        for row in conn.execute(
            "SELECT doc, mask FROM operational_memory_gram_overlay WHERE gram = ?", (gram,)
        )
    ]


def accumulate_relevance(terms: list[str], skip_docs: set[int] | None = None) -> dict[int, int]:
    """Exact relevance per document id for ``terms``.

    ``skip_docs`` holds dirty documents whose base postings are stale; their
    authoritative values come from the overlay instead.
    """
    conn = get_connection()
    weights = _FIELD_WEIGHTS
    accumulator: dict[int, int] = {}
    composite: list[str] = []
    for term in terms:
        if not term:
            continue
        if len(term) <= 2:
            blob = _term_base(term, conn)
            if blob is not None:
                _accumulate(blob, weights, accumulator, skip_docs)
            for doc, mask in _term_overlay(term, conn):
                if mask:
                    accumulator[doc] = accumulator.get(doc, 0) + weights[mask & 0x0F]
        else:
            composite.append(term)
    if composite:
        _accumulate_composite(composite, weights, accumulator)
    return accumulator


def _composite_candidates(term: str, conn) -> set[int]:
    """Documents that could contain ``term`` (every constituent bigram present)."""
    bigrams = {term[i : i + 2] for i in range(len(term) - 1)}
    smallest: set[int] | None = None
    for bigram in bigrams:
        blob = _term_base(bigram, conn)
        docs = {doc for doc, _ in _entries(blob)} if blob is not None else set()
        docs.update(doc for doc, _ in _term_overlay(bigram, conn))
        if not docs:
            return set()
        if smallest is None or len(docs) < len(smallest):
            smallest = docs
    return smallest or set()


def _accumulate_composite(terms: list[str], weights: list[int], accumulator: dict) -> None:
    """Resolve terms of length >= 3 by bigram intersection plus exact verification."""
    conn = get_connection()
    candidates = {term: _composite_candidates(term, conn) for term in terms}
    needed: set[int] = set()
    for docs in candidates.values():
        needed.update(docs)
    if not needed:
        return
    blobs: dict[int, tuple[str, str, str]] = {}
    ordered = sorted(needed)
    for start in range(0, len(ordered), 4000):
        chunk = ordered[start : start + 4000]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT rowid, key_blob, text_blob, page_blob FROM operational_memory_index "
            f"WHERE rowid IN ({placeholders})",
            chunk,
        ):
            blobs[int(row["rowid"])] = (row["key_blob"], row["text_blob"], row["page_blob"])
    for term in terms:
        for doc in candidates[term]:
            fields = blobs.get(doc)
            if fields is None:
                continue
            mask = 0
            if term in fields[0]:
                mask |= 1
            if term in fields[1]:
                mask |= 2
            if term in fields[2]:
                mask |= 4
            if mask:
                accumulator[doc] = accumulator.get(doc, 0) + weights[mask]


def backend_requested() -> str:
    return str(os.environ.get("VECTOR_LAKE_MEMORY_SEARCH", "gram") or "gram").strip().lower()


def memory_gram_index_report() -> str:
    """Human-readable status for the operator surfaces."""
    conn = get_connection()
    init_db()
    state = gram_index_state()
    return (
        f"memory gram index: ready={state['ready']} format={state['format_version']} "
        f"grams={int(state['gram_count'] or 0)} base_docs={int(state['base_docs'] or 0)} "
        f"projection_docs={_projection_doc_count(conn)} "
        f"overlay_rows={overlay_row_count(conn)} "
        f"pending={pending_doc_count(conn)} (live={live_dirty_doc_count(conn)} "
        f"retired={retired_doc_count(conn)}) usable={gram_index_usable()} "
        f"backend={backend_requested()}"
    )
