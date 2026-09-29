"""Two-stage vector index: binary prefilter, then exact full-precision rerank.

Why this shape (measured on the production corpus, 7162 x 3072, ``gemini-embedding-2``):

    vec0 float32 MATCH            227-254 ms   recall@10 1.0000  (today's path, ground truth)
    vec0 bit[3072] hamming           2.04 ms   recall@10 0.7167  (unusable alone)
    bit top-100 -> float rerank      3.38 ms   recall@10 1.0000
    vec0 lookup by ``page_key``      7.7 ms/vector             (trap: never fetch vectors by key)
    plain table lookup by ``rowid``  0.012 ms/vector

So the shortlist must come from a vec0 ``bit`` table (its scan is C-side and the tiny index is
cache-resident), and the rerank must address full-precision vectors by integer ``rowid`` -- an
``IN (page_key)`` fetch against the vec0 table costs more than scanning everything.

The shadow tables are derived data: ``build`` can be re-run at any time, and ``status`` reports
both membership and the recorded projection so a drifted or stale shadow is *unavailable* rather
than silently wrong.  Every write to the shadow happens inside the caller's transaction, so a
half-written vector cannot be observed.
"""

from __future__ import annotations

import logging
import os
import sqlite3

log = logging.getLogger(__name__)

FLOAT_TABLE = "vec_emb_float"
BITS_TABLE = "vec_emb_bits"
META_TABLE = "vec_emb_two_stage_meta"

# Knobs whose defaults come from the measurements above.  ``DEFAULT_SHORTLIST`` is deliberately
# above the 100 that sufficed on the production corpus: binary codes cannot order a crowd of
# near-duplicate vectors (measured: a synthetic 2000-row corpus at pairwise cosine ~0.96 lost true
# tail neighbours -- 0.2 overlap at k=100, 0.4 at k=256, 0.9 at k=1024 -- while the top hit stayed
# correct).  Depth buys margin for such regions; the adaptive "extend until the boundary is not
# inside a tie crowd" rule is the next step, not implemented here.
DEFAULT_SHORTLIST = 256
DEFAULT_FUSION_MODES = ("auto", "two_stage", "legacy")


def mode() -> str:
    """``auto`` (default) uses the shadow when it is present and consistent, else the legacy scan."""
    requested = os.environ.get("VECTOR_LAKE_VECTOR_INDEX", "auto").strip().lower()
    if requested not in DEFAULT_FUSION_MODES:
        log.warning("Unknown VECTOR_LAKE_VECTOR_INDEX=%r; using 'auto'.", requested)
        return "auto"
    return requested


def ensure_schema(conn: sqlite3.Connection, dimension: int | None = None) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {FLOAT_TABLE} ("
        "  rowid INTEGER PRIMARY KEY,"
        "  page_key TEXT NOT NULL UNIQUE,"
        "  embedding BLOB NOT NULL"
        ")"
    )
    if dimension is None:
        _model, dimension = _dimension(conn)
    declared = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (BITS_TABLE,)
    ).fetchone()
    want = f"bits bit[{int(dimension)}]" if dimension else None
    # vec0 fixes the width at CREATE time: a projection at a new dimension must not inherit the
    # old table, or it would reject vectors under a name that still looks correct.
    if declared and want and want not in declared[0]:
        conn.execute(f"DROP TABLE {BITS_TABLE}")
        declared = None
    if not declared:
        if not want:
            raise ValueError("no recorded embedding projection; cannot size the bit index")
        conn.execute(
            f"CREATE VIRTUAL TABLE {BITS_TABLE} USING vec0("
            "  page_key TEXT PRIMARY KEY,"
            f"  {want}"
            ")"
        )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  model TEXT,"
        "  dimension INTEGER,"
        "  row_count INTEGER,"
        "  shortlist INTEGER,"
        "  written_at TEXT"
        ")"
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def _dimension(conn: sqlite3.Connection) -> tuple[str | None, int | None]:
    row = conn.execute(
        "SELECT model, dimension FROM vec_embedding_projection WHERE id = 1"
    ).fetchone()
    if not row:
        return None, None
    return row[0], int(row[1]) if row[1] is not None else None


def status(conn: sqlite3.Connection) -> dict | None:
    """The shadow's recorded state, or ``None`` when it must not be trusted.

    Unavailable is a first-class answer: a missing, half-built, drifted or dimension-mismatched
    shadow falls back to the legacy scan instead of answering from stale vectors.
    """
    for name in (FLOAT_TABLE, BITS_TABLE, META_TABLE):
        if not _table_exists(conn, name):
            return None
    meta = conn.execute(
        f"SELECT model, dimension, row_count, shortlist, written_at FROM {META_TABLE} WHERE id = 1"
    ).fetchone()
    if not meta:
        return None
    model, dimension, row_count, shortlist, written_at = meta
    projected_model, projected_dim = _dimension(conn)
    if projected_model != model or projected_dim != dimension:
        return None
    counts = {
        "float": int(conn.execute(f"SELECT COUNT(*) FROM {FLOAT_TABLE}").fetchone()[0]),
        "bits": int(conn.execute(f"SELECT COUNT(*) FROM {BITS_TABLE}").fetchone()[0]),
        "live": int(conn.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0]),
    }
    # ``mirror_write``/``mirror_delete`` keep the shadow in step with the live table, so the
    # invariant is that the three agree now -- not that they equal the count captured at build
    # time, which any legitimate write after the build would break.
    if len(set(counts.values())) != 1:
        return None
    return {
        "model": model,
        "dimension": dimension,
        "row_count": counts["live"],
        "built_row_count": row_count,
        "shortlist": int(shortlist or DEFAULT_SHORTLIST),
        "written_at": written_at,
    }


def build(conn: sqlite3.Connection, *, shortlist: int = DEFAULT_SHORTLIST) -> dict:
    """(Re)build both shadow tables from ``vec_embeddings``.

    Must be called inside a transaction; the caller commits.  Non-unit vectors are rejected by the
    ``1 - d^2/2`` conversion the product uses, so the copy stays byte-identical to the source.
    """
    ensure_schema(conn)
    model, dimension = _dimension(conn)
    if dimension is None:
        raise ValueError("no recorded embedding projection; refusing to build a blind shadow index")
    conn.execute(f"DELETE FROM {FLOAT_TABLE}")
    conn.execute(f"DELETE FROM {BITS_TABLE}")
    conn.execute(
        f"INSERT INTO {FLOAT_TABLE}(page_key, embedding) SELECT page_key, embedding FROM vec_embeddings"
    )
    conn.execute(
        f"INSERT INTO {BITS_TABLE}(page_key, bits)"
        f" SELECT page_key, vec_quantize_binary(vec_f32(embedding)) FROM vec_embeddings"
    )
    row_count = int(conn.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0])
    conn.execute(
        f"INSERT INTO {META_TABLE}(id, model, dimension, row_count, shortlist, written_at)"
        " VALUES (1, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT(id) DO UPDATE SET model = excluded.model, dimension = excluded.dimension,"
        " row_count = excluded.row_count, shortlist = excluded.shortlist,"
        " written_at = excluded.written_at",
        (model, dimension, row_count, int(shortlist)),
    )
    return {"row_count": row_count, "model": model, "dimension": dimension, "shortlist": shortlist}


def shortlist_keys(
    conn: sqlite3.Connection,
    query_blob: bytes,
    k: int,
    *,
    state: dict | None = None,
) -> list[str]:
    """Binary-prefilter page keys, cheapest possible: no rerank, no scores.

    This is the half of the search that filtering actually needs first -- a caller that will reject
    most candidates on metadata should not pay to score them.  Depth is the caller's ``k``, which is
    also what makes widening meaningful: a deeper call examines more of the corpus.
    """
    rows = conn.execute(
        f"SELECT page_key FROM {BITS_TABLE}"
        " WHERE bits MATCH vec_bit(vec_quantize_binary(vec_f32(?)))"
        " ORDER BY distance LIMIT ?",
        (query_blob, max(1, int(k))),
    ).fetchall()
    return [row[0] for row in rows]


def rerank_keys(
    conn: sqlite3.Connection,
    query_blob: bytes,
    keys,
    limit: int,
) -> list[tuple[str, float]]:
    """Exact L2 over *only* the given keys, ascending; ``[(page_key, distance)]``."""
    keys = list(keys)
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT page_key, vec_distance_l2(embedding, vec_f32(?)) AS d FROM {FLOAT_TABLE}"
        f" WHERE page_key IN ({marks}) ORDER BY d LIMIT ?",
        (query_blob, *keys, max(1, int(limit))),
    ).fetchall()
    return [(row[0], float(row[1])) for row in rows]


def search(
    conn: sqlite3.Connection,
    query_blob: bytes,
    limit: int,
    *,
    state: dict,
) -> list[tuple[str, float]]:
    """``[(page_key, L2 distance)]`` ascending, from the shortlist + exact rerank.

    Distinct L2 distance semantics from the legacy scan: same vectors, same metric, computed on a
    subset that the study showed contains every true top-10 for all 24 measured queries.
    """
    shortlist = max(int(limit), int(state.get("shortlist") or DEFAULT_SHORTLIST))
    return rerank_keys(conn, query_blob, shortlist_keys(conn, query_blob, shortlist, state=state), limit)


def rebuild_report(dry_run: bool = True, shortlist: int = DEFAULT_SHORTLIST) -> str:
    """Human-readable (re)build of the shadow index, for the CLI.

    Recovery path for a shadow that ``status`` reports unavailable -- after a projection change, a
    dimension change, or a first install.  Dry-run reports what would be rebuilt without writing.
    """
    from vector_lake import db_store

    conn = db_store.get_connection()
    before = status(conn)
    live = int(conn.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0])
    model, dimension = _dimension(conn)
    if dry_run:
        return (
            f"[DRY-RUN] shadow index: {'available' if before else 'unavailable'}; "
            f"would rebuild {live} vectors (projection {model}/{dimension}) with shortlist {shortlist}"
        )
    import time

    started = time.perf_counter()
    with db_store.transaction():
        info = build(conn, shortlist=shortlist)
    elapsed = time.perf_counter() - started
    after = status(conn)
    return (
        f"Rebuilt shadow index: {info['row_count']} vectors in {elapsed:.1f}s "
        f"(projection {info['model']}/{info['dimension']}, shortlist {info['shortlist']}); "
        f"status now {'available' if after else 'unavailable'}"
    )


def mirror_write(conn: sqlite3.Connection, page_key: str, query_blob: bytes) -> None:
    """Keep the shadow current for one vector, inside the caller's transaction.

    A no-op when the shadow is absent, so a host that never built it pays nothing (the search path
    falls back on the same condition).
    """
    if not _table_exists(conn, FLOAT_TABLE):
        return
    conn.execute(f"DELETE FROM {FLOAT_TABLE} WHERE page_key = ?", (str(page_key),))
    conn.execute(
        f"INSERT INTO {FLOAT_TABLE}(page_key, embedding) VALUES (?, ?)", (str(page_key), query_blob)
    )
    if _table_exists(conn, BITS_TABLE):
        conn.execute(f"DELETE FROM {BITS_TABLE} WHERE page_key = ?", (str(page_key),))
        conn.execute(
            f"INSERT INTO {BITS_TABLE}(page_key, bits)"
            " VALUES (?, vec_quantize_binary(vec_f32(?)))",
            (str(page_key), query_blob),
        )


def mirror_delete(conn: sqlite3.Connection, page_keys) -> None:
    """Remove shadow rows for vectors that are being deleted, inside the caller's transaction."""
    if not _table_exists(conn, FLOAT_TABLE):
        return
    keys = [(str(key),) for key in page_keys]
    conn.executemany(f"DELETE FROM {FLOAT_TABLE} WHERE page_key = ?", keys)
    if _table_exists(conn, BITS_TABLE):
        conn.executemany(f"DELETE FROM {BITS_TABLE} WHERE page_key = ?", keys)
