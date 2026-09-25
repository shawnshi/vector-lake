"""Which pages are the author's own writing?

The corpus keeps the author's articles, columns and lecture series under ``raw/article/``, and the
ingest ledger already records where each page came from: every ingest job carries the raw
``filepath`` it read and the ``canonical_name`` it wrote.  That pair is the mapping -- nothing here
invents an association.

It is needed because authorship is not otherwise an indexed signal.  Measured on the live corpus,
149 pages come from ``raw/article/`` and **131 of them never mention the author by name** (no 师成,
no Shawn, no Vector Lake, not even 作者), so a query that asks for his view can only reach the 18
that happen to mention him.  For 师成关于医疗人工智能的观点 the pages he actually wrote sat at ranks
12, 13, 14, 17 and 18 while ``Person_Shawn-Shi`` -- a page *about* him -- came second.

Two knobs, both off by default and registered in the README:

* ``VECTOR_LAKE_AUTHOR_SOURCES`` -- which raw prefixes count as the author's own writing, comma
  separated.  Default ``raw/article``.
* ``VECTOR_LAKE_AUTHOR_FACET`` -- ``off`` (default) leaves ranking untouched, ``boost`` lifts the
  score of author pages by ``VECTOR_LAKE_AUTHOR_BOOST`` (default 0.25) times the pool's top score,
  ``filter`` narrows the candidate set to them.
* ``VECTOR_LAKE_AUTHOR_ANNOTATE`` -- default ``1``: the query envelope marks author pages with
  ``[author]``.  The annotation never changes which pages are retrieved.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

log = logging.getLogger(__name__)

AUTHOR_SOURCES_DEFAULT = "raw/article"
AUTHOR_BOOST_DEFAULT = 0.25
AUTHOR_FACET_MODES = ("off", "boost", "filter")
# Both terminal success states: an ingest ends as ``finalized`` or ``completed``.
_SUCCESS_STATES = ("completed", "finalized")

_lock = threading.Lock()
_cache: dict[tuple, frozenset[str]] = {}


def author_source_prefixes() -> tuple[str, ...]:
    """Raw-source prefixes treated as the author's own writing."""
    raw = os.environ.get("VECTOR_LAKE_AUTHOR_SOURCES", AUTHOR_SOURCES_DEFAULT)
    prefixes = []
    for item in str(raw).split(","):
        item = item.strip().strip("/")
        if item:
            prefixes.append(item + "/")
    return tuple(prefixes) or ("raw/article/",)


def author_facet_mode() -> str:
    mode = str(os.environ.get("VECTOR_LAKE_AUTHOR_FACET", "off")).strip().lower()
    if mode not in AUTHOR_FACET_MODES:
        log.warning("Unknown VECTOR_LAKE_AUTHOR_FACET=%r; treating it as off.", mode)
        return "off"
    return mode


def author_boost() -> float:
    """How far an author page is lifted, as a fraction of the pool's top score.

    Clamped into ``[0, 1]``: a relative lift of ``1.0`` already puts an author page level with the
    best candidate in the pool, so a larger value would stop being a preference and become a filter
    with extra steps.
    """
    raw = os.environ.get("VECTOR_LAKE_AUTHOR_BOOST", str(AUTHOR_BOOST_DEFAULT))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("Unparseable VECTOR_LAKE_AUTHOR_BOOST=%r; using %s.", raw, AUTHOR_BOOST_DEFAULT)
        return AUTHOR_BOOST_DEFAULT
    return min(1.0, max(0.0, value))


def annotate_enabled() -> bool:
    raw = str(os.environ.get("VECTOR_LAKE_AUTHOR_ANNOTATE", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _latest_canonical_names(prefixes: tuple[str, ...]) -> dict[str, str]:
    """``raw path -> canonical page`` for the newest successful job of each source.

    The two fields are extracted **in SQL** rather than by ``json.loads`` on every row.  Measured
    2026-09-25 on the live ledger: 3 658 successful ingest jobs hold **156.6 MB** of payload (each
    carries the full ~50 KB ingest prompt), and pulling them into Python to read ``filepath`` and
    ``canonical_name`` cost 1 211 ms versus 495 ms for ``json_extract``.  The payload is the wrong
    place for a hot read to look, but until those two fields have columns of their own this is the
    cheap side of the same read.
    """
    from vector_lake.db_store import get_connection

    connection = get_connection()
    placeholders = ",".join("?" for _ in _SUCCESS_STATES)
    latest: dict[str, tuple[str, str]] = {}
    rows = connection.execute(
        "SELECT json_extract(payload, '$.filepath') AS filepath,"
        " json_extract(payload, '$.canonical_name') AS canonical_name,"
        " completed_at, updated_at FROM jobs"
        f" WHERE task_type = 'ingest' AND status IN ({placeholders})",
        _SUCCESS_STATES,
    )
    for row in rows:
        filepath = str(row["filepath"] or "").replace("\\", "/")
        canonical = str(row["canonical_name"] or "")
        if "raw/" not in filepath or not canonical:
            continue
        relative = "raw/" + filepath.split("raw/", 1)[1]
        if not relative.startswith(prefixes):
            continue
        stamp = str(row["completed_at"] or row["updated_at"] or "")
        if relative not in latest or stamp > latest[relative][1]:
            latest[relative] = (canonical, stamp)
    return {relative: canonical for relative, (canonical, _) in latest.items()}


def author_page_keys(*, refresh: bool = False) -> frozenset[str]:
    """Page keys (filenames without ``.md``) written by the author.

    Cached per database ``data_version``, which SQLite bumps whenever another connection commits,
    so a fresh ingest lands in the next query without a restart.  An unreadable or absent ledger
    yields an empty set: the facet reports nothing rather than guessing from titles.
    """
    from vector_lake.db_store import get_connection

    prefixes = author_source_prefixes()
    try:
        connection = get_connection()
        # Invalidation is keyed on the ledger this facet actually depends on.  ``PRAGMA
        # data_version`` used to be part of the key and it bumps on *any* commit from another
        # connection -- with a running daemon that is every outbox row, embedding batch and status
        # heartbeat, so the cache was effectively per-query.  The jobs ledger cannot be missed this
        # way: an insert or delete moves ``COUNT(*)``, and any update sets ``updated_at`` to now,
        # which moves ``MAX(updated_at)``.  (The read itself is still a scan -- an index on
        # ``jobs(updated_at)`` is the follow-up, measured at 259 ms for this key.)
        ledger = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM jobs"
        ).fetchone()
        key = (
            str(connection.execute("PRAGMA database_list").fetchone()[2]),
            int(ledger[0]),
            str(ledger[1]),
            prefixes,
        )
    except sqlite3.Error as exc:
        log.warning("Author facet unavailable (%s: %s); no author pages.", type(exc).__name__, exc)
        return frozenset()
    with _lock:
        if not refresh and key in _cache:
            return _cache[key]
    try:
        names = _latest_canonical_names(prefixes)
    except sqlite3.Error as exc:
        log.warning("Author facet unavailable (%s: %s); no author pages.", type(exc).__name__, exc)
        return frozenset()
    keys = frozenset(
        name[:-3] if name.endswith(".md") else name for name in names.values() if name
    )
    with _lock:
        _cache[key] = keys
    return keys
