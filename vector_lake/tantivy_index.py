"""tantivy-backed FTS projection, switch-gated behind ``VECTOR_LAKE_FTS``.

Why this exists: the lexical half of retrieval was SQLite's FTS5 for candidate membership plus a
separate BM25 reranker for the pool signal.  tantivy is a Rust BM25 engine with a Python binding
that this project's dependency set can carry
(``tantivy==0.26.2`` has a cp313/win_amd64 wheel, verified 2026-09-25), so one engine can serve
both.

The switch is **default ``fts5``** on purpose.  Candidate ordering inside the returned pool is
relevance-affecting, and this repository's rule for retrieval changes is one switch at a time,
measured against the pre-registered eval harness (``benchmarks/search_eval_decisions*.md``).
Nothing here changes behaviour until an operator sets ``VECTOR_LAKE_FTS=tantivy``.

Contract preserved from the FTS5 path, because callers depend on each part:

* input text is **already tokenized** by the project tokenizer (jieba-rs) and joined by spaces,
  so the analyzer here is whitespace + lowercase -- re-tokenizing would be a second segmenter;
* terms are **ANDed**, and each term matches across ``title``/``summary``/``text`` -- FTS5's
  implicit AND over its three columns;
* ``rank`` keeps FTS5's sign convention: **negative** (``bm25()`` in SQLite is negative), best
  first when sorted ascending -- ``tool_search`` negates it (``raw_score * -1.0``);
* ``node_key`` is a fast field so exact-key lookups and per-candidate scoring stay cheap.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("vector-lake-tantivy")

INDEX_DIRNAME = "tantivy_index"
#: Bumped when the schema or analyzer changes.  A stored value that differs forces a rebuild
#: rather than querying an index whose fields no longer mean what the code assumes.
SCHEMA_VERSION = 1
#: The field the pool rerank signal needs in addition to FTS5's three columns.
SEARCH_FIELDS = ("title", "summary", "text")
ANALYZER = "vl_pretokenized"

_index = None
_writer = None
_searcher = None


def enabled() -> bool:
    """Whether reads and writes should go to tantivy instead of FTS5.

    A missing wheel falls back to FTS5 with a warning rather than failing a page read: the switch
    states an intent, and the projection it would replace is still there and still authoritative.
    """
    if str(os.environ.get("VECTOR_LAKE_FTS", "fts5")).strip().lower() != "tantivy":
        return False
    try:
        import tantivy  # noqa: F401
    except ImportError:
        log.warning("VECTOR_LAKE_FTS=tantivy but the tantivy wheel is not installed; using FTS5")
        return False
    return True


def index_dir() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / INDEX_DIRNAME


def _version_file() -> Path:
    return index_dir() / "vl_schema_version"


def reset() -> None:
    """Forget the cached handles; the next call re-opens (used by tests and after a rebuild)."""
    global _index, _writer, _searcher
    _index = _writer = _searcher = None


def _analyzer(tantivy):
    return (
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.whitespace())
        .filter(tantivy.Filter.lowercase())
        .build()
    )


def _schema(tantivy):
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("node_key", stored=True, tokenizer_name="raw", fast=True)
    for field in ("title", "summary", "aliases", "text"):
        builder.add_text_field(field, stored=True, tokenizer_name=ANALYZER)
    return builder.build()


def open_index(create: bool = True):
    """Open (or create) the index, registering the analyzer.  Cached per process."""
    global _index
    if _index is not None:
        return _index
    import tantivy

    directory = index_dir()
    stored_version = None
    if (directory / "meta.json").exists():
        try:
            stored_version = int(_version_file().read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            stored_version = None

    if stored_version != SCHEMA_VERSION:
        if not create:
            return None
        # A schema/analyzer change or a fresh tree: build a clean index rather than serve one
        # whose fields mean something else.  The corpus is rebuilt from SQLite right after.
        if stored_version is not None:
            log.warning("tantivy index schema %s != %s; rebuilding", stored_version, SCHEMA_VERSION)
        shutil.rmtree(directory, ignore_errors=True)

    directory.mkdir(parents=True, exist_ok=True)
    index = tantivy.Index(_schema(tantivy), path=str(directory))
    index.register_tokenizer(ANALYZER, _analyzer(tantivy))
    _version_file().write_text(str(SCHEMA_VERSION), encoding="utf-8")
    _index = index
    return index


def _get_writer(heap_size: int = 128 * 1024 * 1024):
    """One writer per process; tantivy allows a single writer per index directory.

    The callers write inside ``db_store``'s write lock, so cross-process contention is already
    serialized; this retries briefly for the remaining race (a CLI holding the writer while the
    daemon starts) instead of failing a page write.
    """
    global _writer
    if _writer is not None:
        return _writer
    index = open_index()
    last_error = None
    for attempt in range(5):
        try:
            _writer = index.writer(heap_size=heap_size)
            return _writer
        except Exception as exc:  # noqa: BLE001 - a busy writer is a wait, not a fault
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"could not acquire the tantivy writer: {last_error}")


def _refresh_searcher():
    global _searcher
    index = open_index()
    index.reload()
    _searcher = index.searcher()
    return _searcher


def _document(tantivy, node_key, title, summary, text, aliases=""):
    return tantivy.Document(
        node_key=node_key,
        title=title or "",
        summary=summary or "",
        text=text or "",
        aliases=aliases or "",
    )


def upsert(node_key: str, title: str, summary: str, text: str, aliases: str = "") -> None:
    """Replace one node's document and commit."""
    upsert_many([(node_key, title, summary, text, aliases)])


def upsert_many(rows) -> int:
    """Replace many documents with a single commit (the index-rebuild path)."""
    import tantivy

    writer = _get_writer()
    count = 0
    for row in rows:
        node_key = row[0]
        writer.delete_documents_by_term("node_key", node_key)
        writer.add_document(
            _document(
                tantivy, node_key, row[1], row[2], row[3], row[4] if len(row) > 4 else ""
            )
        )
        count += 1
    if count:
        writer.commit()
    return count


def delete_node(node_key: str) -> None:
    writer = _get_writer()
    writer.delete_documents_by_term("node_key", node_key)
    writer.commit()


def clear() -> None:
    writer = _get_writer()
    writer.delete_all_documents()
    writer.commit()


def search_keys() -> set[str]:
    """Every node_key materialised in this index."""
    searcher = _refresh_searcher()
    keys = set()
    for address in searcher.search(open_index().parse_query("*"), 100_000).hits:
        keys.add(searcher.doc(address[1])["node_key"][0])
    return keys


def search(terms: list[str], limit: int = 50, fields: tuple[str, ...] = SEARCH_FIELDS):
    """``[(node_key, rank)]`` with FTS5's sign convention: a term must match AND that term in at
    least one of ``fields``, and ``rank`` is **minussed** BM25 so ascending order is best-first."""
    import tantivy

    if not terms:
        return []
    index = open_index()
    schema = index.schema
    clauses = []
    for token in terms:
        shoulds = [
            (tantivy.Occur.Should, tantivy.Query.term_query(schema, field, token))
            for field in fields
        ]
        clauses.append((tantivy.Occur.Must, tantivy.Query.boolean_query(shoulds)))
    query = tantivy.Query.boolean_query(clauses)

    index.reload()
    searcher = index.searcher()
    results = searcher.search(query, limit)
    rows = []
    for score, address in results.hits:
        document = searcher.doc(address)
        rows.append((document["node_key"][0], -float(score)))
    return rows


def rebuild_from_sqlite(batch: int = 500) -> int:
    """Populate the index from the authoritative FTS5 table.

    The FTS5 projection already holds pre-tokenized text, so this is the migration path and the
    recovery path after a schema bump -- no re-tokenizing, no page reads.
    """
    from vector_lake import db_store

    db_store.init_db()
    conn = db_store.get_connection()
    clear()
    written = 0
    offset = 0
    while True:
        rows = conn.execute(
            "SELECT node_key, title, summary, text FROM wiki_search_index LIMIT ? OFFSET ?",
            (batch, offset),
        ).fetchall()
        if not rows:
            break
        written += upsert_many(
            [(row["node_key"], row["title"], row["summary"], row["text"], "") for row in rows]
        )
        offset += len(rows)
    log.info("tantivy index rebuilt from FTS5: %d node(s)", written)
    return written


def stats() -> dict:
    index = open_index(create=False)
    if index is None:
        return {"exists": False, "docs": 0, "dir": str(index_dir())}
    # Reload first: a searcher built before the newest commit does not see it, and this is a
    # reporting surface -- it under-reported by exactly the last batch (7139 written, 7000 visible)
    # until the reload was added.
    index.reload()
    return {"exists": True, "docs": index.searcher().num_docs, "dir": str(index_dir())}
