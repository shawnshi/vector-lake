import hashlib
import json
import logging
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from vector_lake.wiki_utils import get_index_path
from vector_lake.wiki_utils import get_meta_dir
import sqlite_vec

log = logging.getLogger("vector-lake-db")

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALIZED_DB_PATHS: set[str] = set()


def _job_idempotency_key(task_type: str, payload: dict | None) -> str | None:
    if task_type != "ingest" or not isinstance(payload, dict):
        return None
    filepath = payload.get("filepath")
    file_hash = payload.get("hash")
    canonical_name = payload.get("canonical_name")
    if not filepath or not file_hash:
        return None
    raw = "\0".join(["ingest", str(filepath), str(file_hash), str(canonical_name or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def get_db_path() -> Path:
    import os
    override = os.environ.get("VECTOR_LAKE_DB_PATH")
    if override:
        return Path(override)
    return get_meta_dir() / "vector_lake.db"

def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the PRAGMAs that SQLite scopes to a single connection.

    ``init_db`` is memoized per database path, so pragmas applied there were
    skipped for every connection opened after ``close_connection`` -- and the
    outbox consumer recycles its connection on each loop iteration.  Foreign key
    enforcement and synchronous mode are connection-scoped, so they silently
    reverted to the SQLite defaults on every recycled connection.
    """
    for statement in (
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            # A read-only or otherwise constrained database must still open.
            logging.getLogger("vector-lake-db").warning("Could not apply %s: %s", statement, exc)


def get_connection() -> sqlite3.Connection:
    if getattr(_LOCAL, "conn", None) is None:
        db_path = get_db_path()
        conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        
        # Load sqlite-vec extension
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        _apply_connection_pragmas(conn)
        
        _LOCAL.conn = conn
    return _LOCAL.conn

def close_connection():
    if hasattr(_LOCAL, "conn") and _LOCAL.conn is not None:
        _LOCAL.conn.close()
        _LOCAL.conn = None
    _LOCAL.in_transaction = False

# --- write-lock budget -------------------------------------------------------
#
# ``connect(timeout=...)`` *is* the SQLite busy timeout, so every
# ``BEGIN IMMEDIATE`` attempt already blocks inside SQLite for up to
# ``DB_BUSY_TIMEOUT_MS`` before it raises ``database is locked``.  The previous
# loop then retried 60 times with a 0.5-1.5 s sleep on top, so one
# ``transaction()`` could park for roughly half an hour while the process looked
# hung: nothing wrote ``.watchdog_status.json`` during the wait, ``runtime_health``
# then reported ``watchdog_stale``, and every writer queued behind it
# (``enqueue_mutation``, ``claim_mutation_outbox``, ingest, GC, timeline rebuild).
#
# The budget below is a hard ceiling on total waiting.  ``PRAGMA busy_timeout``
# is re-armed to the remaining budget before each attempt so the ceiling is real
# rather than nominal, and restored to the default once the lock is taken so
# later one-off statements keep the full patience.
DB_BUSY_TIMEOUT_MS = 30_000

# The MCP client that drives these tools abandons a call at 60 s (the SDK's
# ``DEFAULT_REQUEST_TIMEOUT_MSEC``); the pi adapter passes no per-server override.
# A writer that parks past that point cannot report anything useful -- the caller
# is already gone while the server still holds the request -- so lock contention
# has to surface as a bounded, actionable error well inside the client's patience.
# Keep the two numbers coupled in one place: raising this above
# ``MCP_CALL_TIMEOUT_SECONDS`` silently reintroduces unreportable waits.
MCP_CALL_TIMEOUT_SECONDS = 60.0
BEGIN_LOCK_BUDGET_SECONDS = 20.0
BEGIN_LOCK_MAX_ATTEMPTS = 8


class DatabaseLockTimeout(RuntimeError):
    """The write lock could not be taken inside ``BEGIN_LOCK_BUDGET_SECONDS``.

    Distinct from ``sqlite3.OperationalError`` so callers that already catch
    SQLite errors for a genuinely unusable database do not silently absorb a
    contention timeout, which is a retryable condition with a different remedy.
    """


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _arm_busy_timeout(conn: sqlite3.Connection, remaining_seconds: float) -> None:
    """Point ``PRAGMA busy_timeout`` at the remaining lock budget."""
    budget_ms = max(1, int(min(remaining_seconds, DB_BUSY_TIMEOUT_MS / 1000.0) * 1000))
    try:
        conn.execute(f"PRAGMA busy_timeout={budget_ms:d}")
    except sqlite3.OperationalError as exc:
        log.warning("Could not arm busy_timeout=%s: %s", budget_ms, exc)


def _record_lock_contention(attempts: int, budget_seconds: float, waited_seconds: float, outcome: str) -> None:
    """Persist write-lock contention so a wedge leaves evidence behind.

    SQLite cannot report which connection holds the write lock, so a stalled holder used
    to be indistinguishable from an idle system: every writer silently timed out and the
    only durable trace was a transient stderr line (measured: a held lock wedged all wiki
    writes for hours and needed hand-forensics to attribute).  This writes a bounded
    marker that ``runtime_health`` surfaces, including the caller's function name.
    """
    try:
        import inspect

        from vector_lake.wiki_utils import get_meta_dir

        runtime_dir = get_meta_dir() / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        marker_path = runtime_dir / "write_lock_contention.json"
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except (OSError, json.JSONDecodeError):
            payload = {}
        caller = "unknown"
        for frame in inspect.stack()[2:]:
            if frame.function not in {"_record_lock_contention", "_acquire_write_lock", "transaction"}:
                caller = frame.function
                break
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "attempts": int(attempts),
            "budget_seconds": round(float(budget_seconds), 1),
            "waited_seconds": round(float(waited_seconds), 1),
            "caller": caller,
        }
        payload["last"] = entry
        history = list(payload.get("history") or [])
        history.append(entry)
        payload["history"] = history[-20:]
        marker_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a write
        log.warning("Could not record write-lock contention: %s", exc)


def _acquire_write_lock(conn: sqlite3.Connection) -> None:
    """Take the write lock, or raise :class:`DatabaseLockTimeout` inside the budget."""
    started = time.monotonic()
    deadline = started + BEGIN_LOCK_BUDGET_SECONDS
    last_error: sqlite3.OperationalError | None = None
    attempts = 0
    acquired = False
    while attempts < BEGIN_LOCK_MAX_ATTEMPTS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts += 1
        _arm_busy_timeout(conn, remaining)
        try:
            conn.execute("BEGIN IMMEDIATE")
            acquired = True
            break
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc):
                raise
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Jittered exponential backoff, never past the remaining budget.
            time.sleep(min(remaining, 0.1 * (2 ** (attempts - 1)) * (0.5 + random.random())))
    # Whatever happened, hand the connection back its default patience.
    _arm_busy_timeout(conn, DB_BUSY_TIMEOUT_MS / 1000.0)
    waited = time.monotonic() - started
    if acquired:
        if attempts > 1:
            _record_lock_contention(attempts, BEGIN_LOCK_BUDGET_SECONDS, waited, "acquired-after-retry")
        return
    _record_lock_contention(attempts, BEGIN_LOCK_BUDGET_SECONDS, waited, "timed-out")
    log.error(
        "Could not acquire the Vector Lake write lock within %.0fs (%d attempt(s)); "
        "another writer is holding it. Last error: %s",
        BEGIN_LOCK_BUDGET_SECONDS, attempts, last_error,
    )
    if not acquired:
        raise DatabaseLockTimeout(
            f"Could not acquire the Vector Lake write lock within "
            f"{BEGIN_LOCK_BUDGET_SECONDS:.0f}s ({attempts} attempt(s)); "
            f"another writer is holding it. Last error: {last_error}"
        ) from last_error


@contextmanager
def transaction():
    conn = get_connection()
    if getattr(_LOCAL, "in_transaction", False):
        yield conn
        return

    _acquire_write_lock(conn)
    _LOCAL.in_transaction = True
    try:
        yield conn
        conn.commit()
    except BaseException:
        # A failed ``commit()`` used to escape without a rollback, leaving the
        # connection inside an open transaction; the next ``BEGIN IMMEDIATE`` on
        # it then raised "cannot start a transaction within a transaction",
        # which reads as a schema bug rather than a stuck lock.
        try:
            conn.rollback()
        except sqlite3.Error as rollback_error:
            log.error("Rollback failed, connection state is unknown: %s", rollback_error)
        raise
    finally:
        _LOCAL.in_transaction = False

# --- operational memory search projection -----------------------------------
#
# ``search_operational_memory`` used to decode every row of ``operational_memory``
# (142k rows / ~170 MB of JSON on the live corpus) and score them in Python for
# every query, twice per Memory Packet.  The projection below carries the fields
# the scorer actually reads as plain columns, maintained by triggers so every
# writer -- including ones outside this module -- keeps it in step.
#
# Coercion contract (must stay in step with ``governance_store``):
#   memory_type    : lower(COALESCE(json.memory_type, 'fact'))
#   validity_state : lower(COALESCE(json.validity_state, 'active'))
#   memory_score   : CAST(json.memory_score AS REAL) or 0.0
#   updated_rank   : unixepoch(json.updated_at,'subsec') or 0.0 -- millisecond
#                    precision, while the Python oracle uses microsecond floats,
#                    so rows written inside the same millisecond may tie here
#                    and rank apart in Python.  The differential test in
#                    tests/test_operational_memory_index.py pins this down.
#   *_blob         : lower() of the corresponding JSON field, '' when absent

_OM_INDEX_VALUE_COLUMNS = (
    "memory_type",
    "validity_state",
    "memory_score",
    "updated_rank",
    "key_blob",
    "text_blob",
    "page_blob",
    "source_updated_at",
)

_OM_INDEX_COLUMNS = ("memory_id",) + _OM_INDEX_VALUE_COLUMNS + ("source_rowid",)


def _om_index_value_expr(source: str) -> str:
    """Newline-joined projection value expressions bound to ``source`` (``NEW``/``src``)."""
    return (
        f"            lower(COALESCE(json_extract({source}.data_json, '$.memory_type'), 'fact')),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.validity_state'), 'active')),\n"
        f"            COALESCE(CAST(json_extract({source}.data_json, '$.memory_score') AS REAL), 0.0),\n"
        f"            COALESCE(unixepoch(NULLIF(json_extract({source}.data_json, '$.updated_at'), ''), 'subsec'), 0.0),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.memory_key'), '')),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.text'), '')),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.source_page'), '')),\n"
        f"            COALESCE(json_extract({source}.data_json, '$.updated_at'), ''),\n"
        f"            {source}.rowid"
    )


def _create_operational_memory_index(conn: sqlite3.Connection) -> None:
    """Create the search projection and its maintenance triggers."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_index (
            memory_id TEXT PRIMARY KEY,
            memory_type TEXT NOT NULL,
            validity_state TEXT NOT NULL,
            memory_score REAL NOT NULL,
            updated_rank REAL NOT NULL,
            key_blob TEXT NOT NULL,
            text_blob TEXT NOT NULL,
            page_blob TEXT NOT NULL,
            source_updated_at TEXT NOT NULL DEFAULT '',
            source_rowid INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # ``source_rowid`` reproduces the store's natural ``SELECT *`` order, which is
    # the final tie-break of the Python scorer.  Adding the column to a database
    # that predates it invalidates every existing row, so the rename succeeds and
    # forces one rebuild.
    try:
        conn.execute(
            "ALTER TABLE operational_memory_index "
            "ADD COLUMN source_rowid INTEGER NOT NULL DEFAULT 0"
        )
        stale_format = True
    except sqlite3.OperationalError:
        stale_format = False
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_om_index_type_state "
        "ON operational_memory_index (memory_type, validity_state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_om_index_type ON operational_memory_index (memory_type)"
    )
    columns = ", ".join(_OM_INDEX_COLUMNS)
    upsert = ", ".join(
        f"{column} = excluded.{column}" for column in _OM_INDEX_COLUMNS if column != "memory_id"
    )
    for trigger, event in (
        ("trg_om_index_insert", "AFTER INSERT"),
        ("trg_om_index_update", "AFTER UPDATE"),
    ):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger}
            {event} ON operational_memory
            BEGIN
                INSERT INTO operational_memory_index ({columns})
                VALUES (
                    NEW.memory_id,
{_om_index_value_expr('NEW')}
                )
                ON CONFLICT(memory_id) DO UPDATE SET {upsert};
            END
            """
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_om_index_delete
        AFTER DELETE ON operational_memory
        BEGIN
            DELETE FROM operational_memory_index WHERE memory_id = OLD.memory_id;
        END
        """
    )
    _create_memory_gram_tables(conn)
    _create_page_index_tables(conn)
    _create_claim_index_tables(conn)
    # ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a column
    # added after a schema's first release needs its own tolerated ALTER, exactly
    # as ``operational_memory_index.source_rowid`` does.  ``_table_columns``
    # answers the same question on the read path without a write lock.
    try:
        conn.execute("ALTER TABLE page_index_state ADD COLUMN edge_digest TEXT")
    except sqlite3.OperationalError:
        pass
    ensure_operational_memory_index(conn, force=stale_format)


_CLAIM_INDEX_VALUE_COLUMNS = (
    "claim_type",
    "status",
    "confidence",
    "freshness_tier",
    "valid_to",
    "review_after",
    "evidence_count",
    "contradicts_count",
    "subject_entity_count",
    "source_page",
    "claim_text",
    "source_ids",
)
_CLAIM_INDEX_COLUMNS = ("claim_id",) + _CLAIM_INDEX_VALUE_COLUMNS + ("source_rowid",)


def _claim_count_expr(source: str, field: str) -> str:
    """``len(claim.get(field, []))`` expressed in SQL, type by type.

    ``json_array_length`` raises "malformed JSON" on a scalar and returns NULL for an absent path,
    so each shape is handled explicitly.  ``length()`` on the extracted text reproduces ``len()``
    for a *string* value, which is what the Python oracle would count; a number, a bool or an
    explicit null would make ``len()`` raise there, so 0 is the projection's answer and the
    differential test in ``tests/test_claim_index.py`` pins both behaviours.
    """
    extracted = f"json_extract({source}.data_json, '$.{field}')"
    kind = f"json_type({source}.data_json, '$.{field}')"
    return (
        f"CASE WHEN {kind} = 'array' THEN json_array_length({extracted}) "
        f"WHEN {kind} = 'text' THEN length({extracted}) ELSE 0 END"
    )


def _claim_index_value_expr(source: str) -> str:
    """Projection value expressions bound to ``source`` (``NEW``/``src``), one per column."""
    expressions = (
        f"lower(COALESCE(json_extract({source}.data_json, '$.claim_type'), ''))",
        f"lower(COALESCE(json_extract({source}.data_json, '$.status'), 'Active'))",
        f"COALESCE(CAST(json_extract({source}.data_json, '$.confidence') AS REAL), 0.0)",
        f"lower(COALESCE(json_extract({source}.data_json, '$.freshness_tier'), 'unknown'))",
        f"COALESCE(json_extract({source}.data_json, '$.valid_to'), '')",
        f"COALESCE(json_extract({source}.data_json, '$.review_after'), '')",
        _claim_count_expr(source, "evidence_ids"),
        _claim_count_expr(source, "contradicts"),
        _claim_count_expr(source, "subject_entity_ids"),
        f"COALESCE(json_extract({source}.data_json, '$.source_page'), '')",
        f"lower(COALESCE(json_extract({source}.data_json, '$.claim_text'), ''))",
        f"CASE WHEN json_type({source}.data_json, '$.source_ids') IS NULL THEN NULL "
        f"ELSE json_quote(json_extract({source}.data_json, '$.source_ids')) END",
    )
    separator = "," + chr(10)
    return separator.join("            " + expression for expression in expressions)


def _create_claim_index_tables(conn: sqlite3.Connection) -> None:
    """Schema for the narrow claim projection (``claim_index``).

    Why it exists: ``trace`` scans every claim's text and ``debt`` annotates every claim, and both
    read a fixed handful of fields -- while ``load_claims`` decodes all 101 323 JSON payloads to get
    them (3.2 s of a 4.2 s trace, and the same share of debt).  The projection carries exactly the
    fields those two consume, maintained by triggers the way ``operational_memory_index`` is, so
    the scan and the annotation read columns and only the returned rows are decoded.

    ``source_rowid`` reproduces the store's natural ``SELECT *`` order, which is the order the
    previous full scan iterated in and therefore the tie-break of its stable sort.

    Two columns carry their exact JSON spelling rather than a derived form.  ``source_page`` is
    **not** lowercased, because ``trace`` both lowercases it into a haystack *and* tests it for
    membership in the FTS result set, whose keys are raw; lowercasing here would change that test
    for any page whose key has capitals.  ``source_ids`` is stored as JSON text (not a count) since
    ``debt`` iterates its values, and stays NULL when the key is absent so a reader can apply the
    same ``[]`` default the JSON path did.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_index (
            claim_id TEXT PRIMARY KEY,
            claim_type TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            freshness_tier TEXT NOT NULL,
            valid_to TEXT NOT NULL DEFAULT '',
            review_after TEXT NOT NULL DEFAULT '',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            contradicts_count INTEGER NOT NULL DEFAULT 0,
            subject_entity_count INTEGER NOT NULL DEFAULT 0,
            source_page TEXT NOT NULL DEFAULT '',
            claim_text TEXT NOT NULL DEFAULT '',
            source_ids TEXT,
            source_rowid INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_index_status ON claim_index (status)")
    columns = ", ".join(_CLAIM_INDEX_COLUMNS)
    upsert = ", ".join(
        f"{column} = excluded.{column}" for column in _CLAIM_INDEX_COLUMNS if column != "claim_id"
    )
    for trigger, event in (
        ("trg_claim_index_insert", "AFTER INSERT"),
        ("trg_claim_index_update", "AFTER UPDATE"),
    ):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger}
            {event} ON claims
            BEGIN
                INSERT INTO claim_index ({columns})
                VALUES (
                    NEW.claim_id,
{_claim_index_value_expr("NEW")},
                    NEW.rowid
                )
                ON CONFLICT(claim_id) DO UPDATE SET {upsert};
            END
            """
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_claim_index_delete
        AFTER DELETE ON claims
        BEGIN
            DELETE FROM claim_index WHERE claim_id = OLD.claim_id;
        END
        """
    )
    ensure_claim_index(conn)


def ensure_claim_index(conn: sqlite3.Connection | None = None, force: bool = False) -> dict:
    """Reconcile ``claim_index`` with ``claims``; ``force`` re-derives every row."""
    conn = conn or get_connection()
    result = {
        "canonical": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
        "projected": conn.execute("SELECT COUNT(*) FROM claim_index").fetchone()[0],
        "deleted": 0,
        "rebuilt": 0,
    }
    if not force and result["canonical"] == result["projected"]:
        return result
    columns = ", ".join(_CLAIM_INDEX_COLUMNS)
    if force:
        result["deleted"] = conn.execute("DELETE FROM claim_index").rowcount
        gap_clause = ""
    else:
        result["deleted"] = conn.execute(
            "DELETE FROM claim_index WHERE claim_id NOT IN (SELECT claim_id FROM claims)"
        ).rowcount
        gap_clause = "WHERE src.claim_id NOT IN (SELECT claim_id FROM claim_index)"
    # ``INSERT OR REPLACE ... SELECT`` rather than an upsert: SQLite requires a WHERE clause to
    # disambiguate an ON CONFLICT attached to a SELECT, so the "fill the gaps" path would have to
    # synthesise one.  This mirrors ``ensure_operational_memory_index``.
    cursor = conn.execute(
        f"""
        INSERT OR REPLACE INTO claim_index ({columns})
        SELECT src.claim_id,
{_claim_index_value_expr("src")},
               src.rowid
        FROM claims AS src
        {gap_clause}
        """
    )
    result["rebuilt"] = int(cursor.rowcount or 0)
    return result


def _create_page_index_tables(conn: sqlite3.Connection) -> None:
    """Schema for the ``index.json`` projection in ``vector_lake.page_index_projection``.

    ``page_index_edges`` keeps the published edge multiset together with its ``sequence``, because its
    reader -- ``page_index_projection.adjacency`` -- needs the order for the two-step personalised
    PageRank walk, which is order-sensitive for zero-mass candidates.

    A second table used to hold the same pairs collapsed to ``(source, target, relation)``.  Nothing
    read it but the check that compared the two against each other, so it was removed on 2026-09-18;
    the check now compares this projection against the published file, which is what the contract was
    always about.  Do not reintroduce a table here: the source of truth for edges is ``index.json``
    ``weighted_edges``, and the database holds one projection of it.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_index_nodes (
            node_key TEXT PRIMARY KEY,
            node_id TEXT,
            title TEXT,
            type TEXT,
            status TEXT,
            domain TEXT,
            topic_cluster TEXT,
            node_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_index_nodes_domain ON page_index_nodes (domain)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_index_nodes_cluster ON page_index_nodes (topic_cluster)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_index_edges (
            sequence INTEGER PRIMARY KEY,
            source_key TEXT NOT NULL,
            target_key TEXT NOT NULL,
            weight REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_index_edges_source ON page_index_edges (source_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_index_edges_target ON page_index_edges (target_key)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_index_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            index_mtime REAL NOT NULL DEFAULT 0,
            index_size INTEGER NOT NULL DEFAULT 0,
            node_count INTEGER NOT NULL DEFAULT 0,
            edge_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            edge_digest TEXT
        )
        """
    )


def _create_memory_gram_tables(conn: sqlite3.Connection) -> None:
    """Schema for the exact n-gram index in ``vector_lake.memory_gram_index``.

    The dirty queue is filled by triggers on the projection, so every writer of
    ``operational_memory`` -- including ones outside this module -- marks its own
    documents stale.  Nothing here computes grams: that needs Python, and replacing the
    base is a maintenance step the operator or the scheduled block triggers (see
    ``vector_lake.memory_gram_index``).  The overlay table an older release filled is
    neither created nor read any more, and the prune below drops it where it survives.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_gram (
            gram TEXT PRIMARY KEY,
            postings BLOB NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_abandoned_sources (
            filepath TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            reason TEXT,
            terminal_jobs INTEGER NOT NULL DEFAULT 0,
            abandoned_at TEXT NOT NULL,
            PRIMARY KEY (filepath, file_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_gram_dirty (
            doc INTEGER PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_gram_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            format_version INTEGER NOT NULL DEFAULT 1,
            doc_count INTEGER NOT NULL DEFAULT 0,
            gram_count INTEGER NOT NULL DEFAULT 0,
            postings_count INTEGER NOT NULL DEFAULT 0,
            base_docs INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
        """
    )
    for trigger, event, doc in (
        ("trg_om_gram_dirty_insert", "AFTER INSERT", "NEW.rowid"),
        ("trg_om_gram_dirty_update", "AFTER UPDATE", "NEW.rowid"),
        ("trg_om_gram_dirty_delete", "AFTER DELETE", "OLD.rowid"),
    ):
        # ``INSERT OR IGNORE``/``OR REPLACE`` are rejected here: this trigger runs
        # nested inside the projection's own upsert, and SQLite raises the UNIQUE
        # violation instead of applying the nested conflict resolution (verified
        # with a minimal repro).  An explicit NOT EXISTS guard cannot conflict.
        #
        # Dropped rather than guarded with IF NOT EXISTS because a trigger body is
        # code, and an upgraded database would otherwise keep the previous, buggy
        # body forever.
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(
            f"""
            CREATE TRIGGER {trigger}
            {event} ON operational_memory_index
            BEGIN
                INSERT INTO operational_memory_gram_dirty (doc)
                SELECT {doc} WHERE NOT EXISTS (
                    SELECT 1 FROM operational_memory_gram_dirty WHERE doc = {doc}
                );
            END
            """
        )


def ensure_operational_memory_index(
    conn: sqlite3.Connection | None = None, force: bool = False
) -> dict:
    """Reconcile ``operational_memory_index`` with ``operational_memory``.

    ``force`` re-derives every row (content-drift check); the default only fills
    gaps, which is the cheap path taken on first use and after an out-of-band
    schema upgrade.  Returns row counts so callers can report the work done.
    """
    conn = conn or get_connection()
    result = {
        "canonical": conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0],
        "projected": conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0],
        "deleted": 0,
        "rebuilt": 0,
    }
    if not force and result["canonical"] == result["projected"]:
        return result

    columns = ", ".join(_OM_INDEX_COLUMNS)
    if force:
        result["deleted"] = conn.execute("DELETE FROM operational_memory_index").rowcount
        gap_clause = ""
    else:
        result["deleted"] = conn.execute(
            "DELETE FROM operational_memory_index "
            "WHERE memory_id NOT IN (SELECT memory_id FROM operational_memory)"
        ).rowcount
        gap_clause = (
            "WHERE src.memory_id NOT IN (SELECT memory_id FROM operational_memory_index)"
        )
    result["rebuilt"] = conn.execute(
        f"""
        INSERT OR REPLACE INTO operational_memory_index ({columns})
        SELECT src.memory_id,
{_om_index_value_expr('src')}
        FROM operational_memory AS src
        {gap_clause}
        """
    ).rowcount
    result["projected"] = conn.execute(
        "SELECT COUNT(*) FROM operational_memory_index"
    ).fetchone()[0]
    return result


def operational_memory_index_drift(conn: sqlite3.Connection | None = None) -> dict:
    """Report projection rows that are missing or stale relative to the source."""
    conn = conn or get_connection()
    missing, stale = conn.execute(
        """
        SELECT
            SUM(CASE WHEN idx.memory_id IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN idx.memory_id IS NOT NULL
                      AND idx.source_updated_at <> COALESCE(json_extract(src.data_json, '$.updated_at'), '')
                     THEN 1 ELSE 0 END)
        FROM operational_memory AS src
        LEFT JOIN operational_memory_index AS idx ON idx.memory_id = src.memory_id
        """
    ).fetchone()
    return {"missing": int(missing or 0), "stale": int(stale or 0)}


# Reconciling the projection with the store needs two ``COUNT(*)`` index scans
# (~11 ms on the live 142k-row corpus), which is too much to pay on every query.
# ``PRAGMA data_version`` moves when another connection commits and
# ``Connection.total_changes`` moves for this connection's own writes, so an
# untouched database is cleared by two constant-time reads instead.
_OM_INDEX_GUARD: dict = {"fingerprint": None}


def operational_memory_index_fingerprint(conn: sqlite3.Connection | None = None) -> tuple:
    conn = conn or get_connection()
    return (
        str(get_db_path()),
        conn.execute("PRAGMA data_version").fetchone()[0],
        conn.total_changes,
    )


def ensure_operational_memory_index_cheap(conn: sqlite3.Connection | None = None) -> dict | None:
    """Reconcile only when a write could have invalidated the projection.

    Returns the reconciliation result, or ``None`` when the guard short-circuited.
    """
    conn = conn or get_connection()
    if _OM_INDEX_GUARD["fingerprint"] == operational_memory_index_fingerprint(conn):
        return None
    result = ensure_operational_memory_index(conn)
    _OM_INDEX_GUARD["fingerprint"] = operational_memory_index_fingerprint(conn)
    return result

# --- idempotency index recovery ---------------------------------------------
#
# ``enqueue_mutation`` and ``enqueue_job`` both SELECT by ``idempotency_key`` and
# then INSERT, inside one ``BEGIN IMMEDIATE``.  The write lock is what actually
# serialises them; the unique index is defence-in-depth on top of it, and it is
# the only thing that still holds if a caller forgets the lock.
#
# A database written by an earlier release can already hold duplicate keys.
# Aborting ``init_db`` on such a database would make every command unusable, so
# the full index is skipped -- but skipping it used to be the end of the story,
# which left these tables with *no* uniqueness at all.  The partial index below
# restores exactly the guarantee a concurrent enqueue can violate, and it does so
# without deleting any history.
#
# The predicate is written as "not a terminal state" rather than a list of active
# states, so a status added later is protected by default instead of silently
# falling outside the index.
_TERMINAL_IDEMPOTENCY_STATES = ("completed", "finalized", "cancelled", "superseded")
_ACTIVE_IDEMPOTENCY_PREDICATE = (
    "idempotency_key IS NOT NULL AND COALESCE(status, '') NOT IN ("
    + ", ".join(f"'{state}'" for state in _TERMINAL_IDEMPOTENCY_STATES)
    + ")"
)

# table -> (unique index name, primary-key column).  The primary key differs per
# table: ``mutation_outbox`` uses ``id``, ``jobs`` uses ``job_id``.  The repair has
# to know which, because "the canonical row is the lowest one" is expressed against
# it and hardcoding ``id`` made the ``jobs`` repair fail with "no such column: id".
IDEMPOTENCY_TABLES: tuple[str, ...] = ("mutation_outbox", "jobs")
_IDEMPOTENCY_INDEXES: dict[str, tuple[str, str]] = {
    "mutation_outbox": ("idx_mutation_outbox_idempotency", "id"),
    "jobs": ("idx_jobs_idempotency", "job_id"),
}


def _duplicate_idempotency_groups(conn: sqlite3.Connection, table: str) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM (SELECT idempotency_key FROM {table} "
            f"WHERE idempotency_key IS NOT NULL "
            f"GROUP BY idempotency_key HAVING COUNT(*) > 1)"
        ).fetchone()[0]
    )


def _ensure_idempotency_index(
    conn: sqlite3.Connection, table: str, index_name: str
) -> str:
    """Create the strongest idempotency index the current data allows.

    Returns ``full`` (every key unique forever), ``active`` (unique among
    in-flight rows only, because the table already holds duplicate history) or
    ``absent`` (uniqueness is solely the surrounding write lock).
    """
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
            f"ON {table}(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        return "full"
    except sqlite3.IntegrityError:
        pass

    duplicates = _duplicate_idempotency_groups(conn, table)
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name}_active "
            f"ON {table}(idempotency_key) WHERE {_ACTIVE_IDEMPOTENCY_PREDICATE}"
        )
    except sqlite3.IntegrityError:
        log.error(
            "%s holds %s duplicated idempotency_key group(s), and at least one duplicate "
            "is still in a non-terminal status, so no unique index could be created. "
            "Concurrent enqueues are protected only by the BEGIN IMMEDIATE write lock. "
            "Resolve the in-flight duplicates, then re-run init_db().",
            table,
            duplicates,
        )
        return "absent"

    log.warning(
        "%s holds %s duplicated idempotency_key group(s) written by an earlier release, "
        "so the full unique index cannot be created. Created %s_active over non-terminal "
        "rows instead, which is the population a concurrent enqueue can collide in. "
        "Call repair_idempotency_keys(%r, dry_run=False) to clear the redundant keys and "
        "reclaim the full index.",
        table,
        duplicates,
        index_name,
        table,
    )
    return "active"


def idempotency_index_state() -> dict[str, dict]:
    """Report the uniqueness level actually in force for each idempotency table."""
    init_db()
    conn = get_connection()
    existing = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    state: dict[str, dict] = {}
    for table, (index_name, _primary_key) in _IDEMPOTENCY_INDEXES.items():
        if index_name in existing:
            uniqueness = "full"
        elif f"{index_name}_active" in existing:
            uniqueness = "active"
        else:
            uniqueness = "absent"
        state[table] = {
            "uniqueness": uniqueness,
            "duplicate_groups": _duplicate_idempotency_groups(conn, table),
        }
    return state


def repair_idempotency_keys(table: str, dry_run: bool = True) -> dict:
    """Clear the *redundant* idempotency keys so the full unique index is available.

    The canonical row for a key is the one ``enqueue_mutation`` / ``enqueue_job``
    already returns for it: the lowest ``id``.  Every other row keeps its status,
    timestamps, error text and ``superseded_by`` link -- only the duplicate key is
    cleared, which is what makes the rows collide in the first place.  No audit
    history is deleted, and ``dry_run`` (the default) reports the exact ids first.
    """
    if table not in _IDEMPOTENCY_INDEXES:
        raise ValueError(f"Unsupported idempotency table: {table}")
    index_name, primary_key = _IDEMPOTENCY_INDEXES[table]
    init_db()
    conn = get_connection()
    with transaction():
        rows = conn.execute(
            f"SELECT {primary_key} FROM {table} "
            f"WHERE idempotency_key IS NOT NULL AND {primary_key} NOT IN ("
            f"SELECT MIN({primary_key}) FROM {table} WHERE idempotency_key IS NOT NULL "
            f"GROUP BY idempotency_key)"
        ).fetchall()
        # The primary key is not always an integer: ``mutation_outbox`` uses an
        # INTEGER ``id`` while ``jobs`` uses a hex ``job_id``, so the values are
        # carried through untyped rather than coerced.
        redundant_keys = [row[primary_key] for row in rows]
        result = {
            "table": table,
            "dry_run": bool(dry_run),
            "redundant_rows": len(redundant_keys),
            "redundant_keys": redundant_keys,
            "duplicate_groups_before": _duplicate_idempotency_groups(conn, table),
            "uniqueness_before": idempotency_index_state()[table]["uniqueness"],
        }
        if dry_run or not redundant_keys:
            result["uniqueness_after"] = result["uniqueness_before"]
            return result
        conn.executemany(
            f"UPDATE {table} SET idempotency_key = NULL WHERE {primary_key} = ?",
            [(row_id,) for row_id in redundant_keys],
        )
        result["uniqueness_after"] = _ensure_idempotency_index(conn, table, index_name)
        result["duplicate_groups_after"] = _duplicate_idempotency_groups(conn, table)
        return result


# Objects that prove ``_init_db_once`` ran to completion.  Its DDL is one
# transaction, so a partial schema cannot exist: either the whole tail is there
# or nothing was committed.  The list deliberately mixes early and late objects
# so a database created by an older, narrower version still takes the full path.
_SCHEMA_SENTINELS: tuple[str, ...] = (
    "entities",
    "claims",
    "operational_memory",
    "operational_memory_index",
    "wiki_search_index",
    "page_index_nodes",
    "page_index_edges",
    "page_index_state",
    # A new table has to be a sentinel, or ``_schema_is_complete`` reports an existing
    # database as complete and the DDL that creates it never runs.
    "ingest_abandoned_sources",
    "claim_index",
    "vec_embedding_projection",
)


# ---------------------------------------------------------------------------
# One-time schema prunes
# ---------------------------------------------------------------------------
# Objects that no release in this tree can create, read, write or clean.  Each
# one was left behind by a release whose modules were removed wholesale: the
# live database still carries it because ``CREATE ... IF NOT EXISTS`` wrote it
# once and nothing ever issued a DROP.  Being invisible from inside the tree is
# exactly why they survived several audits.
#
# 2026-09-16, 526df6a ("Local tree becomes the authoritative main line") deleted
# 23 856 lines including the whole v6 retention layer and its test file.  What
# it could not delete is the schema it had already written into live databases:
#
#   ``ingest_jobs`` + its two indexes
#       No SQL in this tree names the table.  The token survives only inside the
#       function name ``requeue_legacy_ingest_jobs``, which queries ``jobs``.
#   ``idx_jobs_retention_v6``, ``idx_mutation_outbox_retention_v6``
#       Built to serve retention DELETEs that no longer exist -- which is why
#       ``mutation_outbox`` (32 280 terminal rows, oldest 2026-07-12) and
#       ``jobs`` have grown unbounded since.
#   ``idx_mutation_outbox_idempotency_lookup``
#       ``_ensure_idempotency_index`` creates ``{name}`` and ``{name}_active``,
#       never ``_lookup``.
#   ``claim_graph_nodes``
#       Zero rows.  The tree creates it and DELETEs from it in the canonical
#       cascade, but never INSERTs into it and never reads it.
#   ``change_sets.change_id``
#       NULL in all 26 774 rows, and the string ``change_id`` appears in no
#       Python file in the repository.
#
# Recovery point: the pre-prune DDL for every object above was captured before
# the first run.  Recreating them is pure DDL and no row data is at risk in
# either direction -- the two tables hold zero rows and the dropped column is
# NULL everywhere.
def _prune_orphaned_legacy_schema(conn: sqlite3.Connection) -> None:
    """Drop the objects a removed release created but never dropped."""
    for statement in (
        "DROP TABLE IF EXISTS ingest_jobs",
        "DROP INDEX IF EXISTS idx_jobs_retention_v6",
        "DROP INDEX IF EXISTS idx_mutation_outbox_retention_v6",
        "DROP INDEX IF EXISTS idx_mutation_outbox_idempotency_lookup",
        "DROP TABLE IF EXISTS claim_graph_nodes",
    ):
        conn.execute(statement)


#: The prune that removes the gram overlay table.  Named once because the read path checks
#: for it: a database whose schema has not converged must not serve from that index.
GRAM_OVERLAY_DROP = "2026-09-18-drop-gram-overlay"


def _prune_gram_overlay(conn: sqlite3.Connection) -> None:
    """Drop the overlay table a released incremental path used to write.

    Nothing creates it, writes it or reads it any more (``memory_gram_index`` explains why
    it was wrong, not merely unused), so a database that still has it converges here.  The
    index on it is dropped with the table.

    Convergence is one-shot per database: the ledger skips a migration it has recorded, so a
    table re-created *afterwards* by a process of a removed release is not dropped again, and
    no surface reports it.  Re-running the DROP on every ``init_db()`` would take the write
    lock on every start, which is the cost the ledger exists to avoid.
    """
    conn.execute("DROP TABLE IF EXISTS operational_memory_gram_overlay")


def _prune_entities_json_indexes(conn: sqlite3.Connection) -> None:
    """Drop the three indexes the ``entities`` json queries used to need.

    ``$.page_key`` is now a generated column with its own index, and ``$.type``/``$.status`` are
    read through the real ``type``/``status`` columns wherever they are read at all -- no statement
    in the tree filters ``entities`` by either one (checked by grep, not by a query trace, which
    would only say the tests never issued that shape).  They cost a write on every entity insert.
    Recreatable from the DDL if a future query needs them.
    """
    for statement in (
        "DROP INDEX IF EXISTS idx_entities_type",
        "DROP INDEX IF EXISTS idx_entities_status",
        "DROP INDEX IF EXISTS idx_entities_page_key",
    ):
        conn.execute(statement)


def _prune_entities_type_status_index(conn: sqlite3.Connection) -> None:
    """Drop the last unused index on ``entities``, under its own migration name.

    ``archive/migrate_v9_to_v10.py`` created ``idx_entities_type_status(type, status)`` and the
    current DDL does not, so a database that ran that migration still carries it.  No statement in
    the tree filters ``entities`` by type or status (checked by grep), so it is a write on every
    entity insert and serves nothing.

    A separate name rather than a step added to the json-index prune: the ledger skips a migration
    it has already recorded, so adding a step there would never run on the databases that matter.
    """
    conn.execute("DROP INDEX IF EXISTS idx_entities_type_status")


def _prune_om_json_indexes(conn: sqlite3.Connection) -> None:
    """Retire the four json indexes on ``operational_memory``.

    Two are replaced by indexes on generated columns of the same data
    (``idx_om_f_memory_key``, ``idx_om_f_source_claim``, created by the DDL that ran before this
    prune).  The other two index ``$.memory_type`` and ``$.status``, and *no statement in the tree
    filters on those expressions* -- the real ``memory_type``/``status`` columns are what statements
    name, and an expression index cannot serve a bare-column predicate, so those two were
    unreachable rather than merely unused.

    The real-column indexes (``idx_om_type``, ``idx_om_status``, ``idx_memory_type_status``) are
    deliberately left alone: no statement in the tree filters *this table* by those columns today
    (the four ``memory_type IN (...)`` sites filter ``operational_memory_index``), but removing an
    index was measured harmful in general (9.68 -> 632.74 ms for a status filter once removed), and
    ``idx_om_type`` is now a strict prefix of ``idx_om_f_memory_key``.  Whether they earn their
    write cost is a question about the search path, not about this migration.
    """
    # The two unreachable json indexes go unconditionally: no statement can use them whatever else
    # exists.  The two that are *replaced* go only when their replacement is present -- the DDL that
    # creates it sits inside a warning-only ``except OperationalError`` and the fast path never
    # re-runs it, so dropping first would leave the claim/delete paths scanning 146k rows with no way
    # back.  Probing is one read of ``sqlite_master``.
    present = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for statement in ("DROP INDEX IF EXISTS idx_memory_type", "DROP INDEX IF EXISTS idx_memory_status"):
        conn.execute(statement)
    for old_index, replacement in (
        ("idx_memory_key", "idx_om_f_memory_key"),
        ("idx_memory_source_claim", "idx_om_f_source_claim"),
    ):
        if replacement in present:
            conn.execute(f"DROP INDEX IF EXISTS {old_index}")


def _backfill_entities_ttl(conn: sqlite3.Connection) -> None:
    """Fill ``entities.ttl`` from the json it was supposed to mirror, once.

    ``upsert_entity`` and ``save_entities`` have always written the column from the record's
    ``ttl``, but the batch path omitted it from its ``INSERT OR REPLACE``, which resets any column
    the statement does not name.  The json is the source the indexer reads, so where it holds a
    ``ttl`` and the column is NULL, the column is the one that is wrong.  Idempotent: after the
    first run the ``WHERE`` matches nothing.

    ``decay_weight`` has no json counterpart at all, so there is nothing to backfill it from and
    this deliberately does not invent a value; both write paths now write it consistently instead.
    """
    conn.execute(
        "UPDATE entities SET ttl = CAST(json_extract(data_json, '$.ttl') AS REAL) "
        "WHERE ttl IS NULL AND json_extract(data_json, '$.ttl') IS NOT NULL"
    )


def _prune_claim_evidence_json_indexes(conn: sqlite3.Connection) -> None:
    """Retire the three expression indexes those tables' columns replace.

    Each is replaced by an index on the generated column holding the same expression, created by the
    DDL that ran before this prune; as with ``operational_memory`` the drop is conditional on the
    replacement existing, because the DDL is best-effort and the fast path never re-runs it.
    """
    present = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for old_index, replacement in (
        ("idx_claims_page_key", "idx_claims_f_page_key"),
        ("idx_claims_claim_type", "idx_claims_f_claim_type"),
        ("idx_evidence_page_key", "idx_evidence_f_page_key"),
    ):
        if replacement in present:
            conn.execute(f"DROP INDEX IF EXISTS {old_index}")


def _prune_governance_queue_index(conn: sqlite3.Connection) -> None:
    """Drop the expression index on ``governance_queue`` -- no statement can use it.

    The table is read through the generic ``_load_db_queue`` helper, which selects every row and
    filters nothing, so the tree contains no predicate on either expression this index is built from
    (``$.change_set_id``, ``$.status``).  Unreachable, not merely unused -- the structural form of the
    argument the earlier index-slimming attempt lacked.  The current DDL does not create it (an older
    release did), so a prune is the only way it converges.
    """
    conn.execute("DROP INDEX IF EXISTS idx_governance_queue_change_set_status")


def _prune_page_graph_edges(conn: sqlite3.Connection) -> None:
    """Drop the second SQLite copy of the published edge set.

    Nothing read it: the search projection is ``page_index_edges``, and the only consumer of this
    table was the consistency check that compared the two against each other, now rewired to compare
    ``page_index_edges`` against the published file instead.  Its indexes go with it.
    """
    conn.execute("DROP TABLE IF EXISTS page_graph_edges")


def _normalise_entities_ttl_encoding(conn: sqlite3.Connection) -> None:
    """Give "absent" one encoding in ``entities.ttl``/``decay_weight``: ``0.0``.

    Both writers store ``0.0`` when a record carries no value, so a NULL in these columns means the
    row has simply not been written since they were added as real columns -- two spellings of one
    state, which is how a column ends up meaning different things depending on its history.  The
    json is the source the indexer reads, and it is untouched here; ``decay_weight`` has no json
    counterpart, so ``0.0`` is the writers' own value for "not set".

    Idempotent: after the first run the ``WHERE`` matches nothing.
    """
    conn.execute(
        "UPDATE entities SET ttl = 0.0 WHERE ttl IS NULL AND json_extract(data_json, '$.ttl') IS NULL"
    )
    conn.execute("UPDATE entities SET decay_weight = 0.0 WHERE decay_weight IS NULL")


def _prune_change_sets_change_id(conn: sqlite3.Connection) -> None:
    """Drop ``change_sets.change_id`` while a pre-prune database still has it.

    Guarded by a read-only probe, deliberately not by catching
    ``OperationalError``.  A ``DROP COLUMN`` that fails because the column is
    referenced by an index, trigger or view reports
    ``error in index ... after drop column: no such column: ...`` -- a substring
    test for ``no such column`` swallows exactly that and then records the
    migration as applied while the column is still there.  With the probe there
    is nothing to tolerate: the statement is issued only when the column exists,
    so any failure from it is real and must propagate.
    """
    if "change_id" not in _table_columns(conn, "change_sets"):
        return
    conn.execute("ALTER TABLE change_sets DROP COLUMN change_id")


# Each migration is a name plus ordered steps.  Steps are callables rather than
# SQL strings so a step can make a decision (probe a column) instead of relying
# on an error message to tell "already done" apart from "failed".
def _restore_processed_files_observation_snapshot(conn: sqlite3.Connection) -> None:
    """Restore the ledger's observation snapshot columns.

    ``processed_files`` carried ``observed_mtime_ns``/``observed_size`` until the 2026-09-17
    refactor dropped the writer.  The columns stayed in the live database, this DDL reverted
    to three columns, and no migration recorded either the addition or the abandonment -- so
    a fresh database has three columns while the operator's database had five.

    What replaced them was unsound: the scan gate compared the file's mtime against the
    row's wall-clock ``processed_at``.  A file edited after finalize can still carry an mtime
    a few milliseconds *earlier* than that row (NTFS clock granularity is ~15.6 ms), and the
    gate then skipped the edit permanently -- measured 2026-09-20: a 3.9 ms inversion hid a
    real edit, and the same gate silently ignores any change restored with an older mtime
    (``cp -p``, ``git checkout``).  The snapshot makes "unchanged" an observation rather than
    a clock comparison, and the content hash stays the authority whenever it disagrees.
    """
    present = _table_columns(conn, "processed_files")
    if not present:
        return
    for column in ("observed_mtime_ns", "observed_size"):
        if column not in present:
            conn.execute(f"ALTER TABLE processed_files ADD COLUMN {column} INTEGER")


_LEGACY_SCHEMA_PRUNES: tuple[
    tuple[str, tuple[Callable[[sqlite3.Connection], None], ...]], ...
] = (
    ("2026-09-17-prune-orphaned-legacy-schema", (_prune_orphaned_legacy_schema,)),
    ("2026-09-17-drop-change-sets-change-id", (_prune_change_sets_change_id,)),
    (GRAM_OVERLAY_DROP, (_prune_gram_overlay,)),
    ("2026-09-18-entities-json-indexes", (_prune_entities_json_indexes,)),
    ("2026-09-18-entities-type-status-index", (_prune_entities_type_status_index,)),
    ("2026-09-18-om-json-indexes", (_prune_om_json_indexes,)),
    ("2026-09-18-backfill-entities-ttl", (_backfill_entities_ttl,)),
    ("2026-09-18-claim-evidence-json-indexes", (_prune_claim_evidence_json_indexes,)),
    ("2026-09-18-governance-queue-index", (_prune_governance_queue_index,)),
    ("2026-09-18-normalise-entities-ttl-encoding", (_normalise_entities_ttl_encoding,)),
    ("2026-09-18-drop-page-graph-edges", (_prune_page_graph_edges,)),
    (
        "2026-09-20-restore-processed-files-observation-snapshot",
        (_restore_processed_files_observation_snapshot,),
    ),
)

_SCHEMA_MIGRATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        name TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
"""


def _ledger_exists(conn: sqlite3.Connection) -> bool:
    """Read-only: is the prune ledger table there at all?"""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        is not None
    )


def _recorded_prunes(conn: sqlite3.Connection) -> set[str]:
    """Read-only: the migration names in the ledger.

    Empty when the ledger table does not exist, which is the state of every
    database written before the first prune ran.  Takes no write lock, so it is
    safe to call before deciding whether a transaction is needed at all.

    Table absence is probed through ``sqlite_master`` rather than caught as an
    error.  Swallowing the read and returning "empty" would turn any transient
    failure -- a lock, a schema change in flight -- into "nothing has been
    applied yet", and the caller would then re-run finished migrations and
    overwrite the ledger timestamps with new ones.
    """
    if not _ledger_exists(conn):
        return set()
    return {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}


def apply_legacy_schema_prunes() -> list[str]:
    """Drop schema left behind by removed releases.  Idempotent and recorded.

    Reads the ledger before deciding anything, so a converged database costs one
    SELECT and takes no write lock at all.  Each migration owns its own
    transaction, so an earlier success survives a later failure.  A migration is
    recorded only after every one of its steps succeeded, so a failure leaves it
    pending for the next attempt instead of reporting success.

    Returns the migration names this call actually applied.
    """
    conn = get_connection()
    pending = [
        (name, steps)
        for name, steps in _LEGACY_SCHEMA_PRUNES
        if name not in _recorded_prunes(conn)
    ]
    completed: list[str] = []
    for name, steps in pending:
        with transaction():
            conn = get_connection()
            conn.execute(_SCHEMA_MIGRATIONS_DDL)
            if name in _recorded_prunes(conn):
                continue
            for step in steps:
                step(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, datetime.now(timezone.utc).isoformat()),
            )
            completed.append(name)
    return completed


def legacy_schema_prune_names() -> tuple[str, ...]:
    """Names of the recorded prunes, in application order."""
    return tuple(name for name, _ in _LEGACY_SCHEMA_PRUNES)


def applied_schema_prunes(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """The prune ledger, for the doctor surface.  Empty when never applied.

    Deliberately does not call ``init_db()``: this is a diagnostic, and a
    diagnostic that can write is not a diagnostic.
    """
    if conn is None:
        conn = get_connection()
    if not _ledger_exists(conn):
        return {}
    rows = conn.execute("SELECT name, applied_at FROM schema_migrations").fetchall()
    return {row["name"]: row["applied_at"] for row in rows}


def _apply_prunes_best_effort() -> None:
    """Run the prunes without making them a precondition for using the database.

    ``init_db()`` never needed the write lock when the schema was already
    complete, and every command goes through it.  A read-only snapshot, or a
    database whose writer is busy elsewhere, must stay usable: an un-applied
    cleanup degrades to a warning, and ``doctor`` reports the schema as not
    converged.  ``DatabaseLockTimeout`` is deliberately not caught -- it is
    raised only after this module's own lock budget, and its class docstring
    records that callers must not absorb it.
    """
    try:
        apply_legacy_schema_prunes()
    except sqlite3.OperationalError as exc:
        log.warning("Legacy schema prune deferred, schema not converged: %s", exc)


def _schema_is_complete(db_path: Path) -> bool:
    """Read-only probe: every sentinel object already exists.

    Opens a bare connection and reads ``sqlite_master`` only.  No PRAGMAs are
    applied and no statement is issued that could take the write lock, so this is
    safe to call on the read path while another process is writing.  The caller
    uses it to skip the DDL transaction that would otherwise make every read wait
    behind the writer holding ``BEGIN IMMEDIATE``.

    The prune ledger is deliberately *not* part of this contract.  Requiring it
    here would make ``init_db()`` fail outright on a read-only snapshot of a
    pre-prune database, when all that is missing is a cleanup.  The prunes are
    run separately by ``_apply_prunes_best_effort``, which is how a database
    whose sentinels were already present still converges on its next
    ``init_db()`` instead of never.
    """
    if not db_path.exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        present = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        }
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return set(_SCHEMA_SENTINELS).issubset(present)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``; empty when the table does not exist."""
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _table_xcolumns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table`` *including* generated ones; empty when it does not exist.

    ``PRAGMA table_info`` omits a ``GENERATED`` column -- only ``table_xinfo`` lists it -- so a
    probe built on :func:`_table_columns` cannot see one and would report it missing forever,
    re-running the ``ALTER`` on every start until it failed with "duplicate column name".
    """
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_xinfo({table})")}
    except sqlite3.Error:
        return set()


def _om_index_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only equivalent of ``_init_db_once``'s ``ALTER TABLE`` format probe.

    That probe learned whether ``source_rowid`` was missing by attempting the
    ALTER, which requires the write lock.  ``PRAGMA table_info`` answers the same
    question without it, so the staleness decision can be made before deciding
    whether the write transaction is needed at all.  A missing table reports
    stale, which sends the caller down the full initialisation path.
    """
    return "source_rowid" not in _table_columns(conn, "operational_memory_index")


def _entities_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: is the generated column the ``page_key`` queries use missing?

    Columns added by ``_init_db_once`` need this, or a database that already has every sentinel
    takes the fast path and never runs the ``ALTER`` -- which is how the rewritten queries found
    themselves asking for a column that did not exist yet.  Same shape as
    :func:`_om_index_format_is_stale`.
    """
    if "f_page_key" not in _table_xcolumns(conn, "entities"):
        return True
    return "idx_entities_f_page_key" not in _index_names(conn)


def _index_names(conn: sqlite3.Connection) -> set[str]:
    """Read-only: every index name in the database."""
    try:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    except sqlite3.Error:
        return set()


def _om_probability_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: are the generated columns the memory queries name missing?

    The indexes are checked with the columns, not only the columns.  The ``CREATE INDEX`` statements
    live in one ``try/except OperationalError`` that only logs, so a failure there -- a full disk, a
    limit -- skips the rest of the block; the fast path never re-runs it, and the conditional prunes
    then *keep* the retired expression indexes, which cannot serve `f_page_key = ?`.  The result
    would be correct answers at full-scan cost, permanently and with no operator-visible signal.
    Requiring the replacement index makes the DDL run again instead.
    """
    present = _table_xcolumns(conn, "operational_memory")
    if not {"f_memory_key", "f_source_claim_id"} <= present:
        return True
    return not {"idx_om_f_memory_key", "idx_om_f_source_claim"} <= _index_names(conn)


def _claims_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: are the generated columns the claim/evidence/change-set queries name missing?"""
    expected = {
        "claims": ({"f_claim_type", "f_page_key", "f_source_page"},
                   {"idx_claims_f_claim_type", "idx_claims_f_page_key", "idx_claims_f_source_page"}),
        "evidence": ({"f_page_key"}, {"idx_evidence_f_page_key"}),
        "change_sets": ({"f_status", "f_idempotency_key"},
                        {"idx_change_sets_f_status", "idx_change_sets_f_idempotency"}),
    }
    indexes = _index_names(conn)
    for table, (columns, index_names) in expected.items():
        if not columns <= _table_xcolumns(conn, table):
            return True
        if not index_names <= indexes:
            return True
    return False


def _page_index_state_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Whether ``page_index_state`` still lacks the edge digest column."""
    return "edge_digest" not in _table_columns(conn, "page_index_state")


def init_db():
    db_path = get_db_path()
    db_key = str(db_path.resolve())
    if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
        return
    with _INIT_LOCK:
        if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
            return
        if (
            _schema_is_complete(db_path)
            and not _om_index_format_is_stale(get_connection())
            and not _page_index_state_format_is_stale(get_connection())
            and not _entities_format_is_stale(get_connection())
            and not _om_probability_format_is_stale(get_connection())
            and not _claims_format_is_stale(get_connection())
        ):
            # The DDL would be a no-op, and running it would block every reader
            # behind the writer's lock.  Keep the cheap gap-fill so a writer that
            # bypassed the triggers still gets reconciled, then memoise.
            _INITIALIZED_DB_PATHS.add(db_key)
            ensure_operational_memory_index(get_connection())
            _apply_prunes_best_effort()
            return
        _INITIALIZED_DB_PATHS.discard(db_key)
        _init_db_once(db_key)
        _apply_prunes_best_effort()


def _init_db_once(db_key: str):
    conn = get_connection()
    with transaction():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                canonical_name TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            -- ``entity_id`` holds a **page key** (``Concept_...``), which is what every caller passes
            -- and what ``delete_embedding`` matches on.  It is not ``entities.entity_id``: that column
            -- is a different identifier, so a join between the two returns nothing and does so
            -- silently.  The FTS table keys by ``node_key``, so vector hits and lexical hits stay in
            -- one namespace.  Renaming this column is not an ``ALTER`` -- vec0 refuses both RENAME and
            -- ADD COLUMN -- so the fix is a staged rebuild, not yet done.
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                entity_id TEXT PRIMARY KEY,
                embedding float[3072]
            )
        """)
        conn.execute("""
            -- Which model produced the projection currently in ``vec_embeddings``.
            --
            -- ``embedding_runs`` records the model per *run*, so once more than one run exists it
            -- cannot answer "which model produced the vectors now stored", and that is the question
            -- that matters: changing ``VECTOR_LAKE_EMBEDDING_MODEL`` moves the query encoder while
            -- the stored vectors stay where they are, so every similarity quietly becomes
            -- meaningless with nothing to compare against.  vec0 refuses extra columns, hence a
            -- separate single-row marker rather than per-row provenance.
            CREATE TABLE IF NOT EXISTS vec_embedding_projection (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                model TEXT,
                dimension INTEGER,
                written_at TEXT
            )
        """)
        for col, col_type in [("type", "TEXT"), ("status", "TEXT"), ("ttl", "INTEGER"), ("decay_weight", "REAL")]:
            try:
                conn.execute(f"ALTER TABLE entities ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass
        # ``page_key`` is named by nine statements across six modules (thirteen source lines) and
        # was only ever reachable through
        # ``json_extract``, so it gets a *generated* column: unlike the four above (which a writer
        # has to keep in step, and which nothing but a full rewrite can backfill), a virtual
        # column is by construction always equal to the json it derives from -- there is no drift
        # to compare, no trigger, and no separate projection table to keep consistent.
        if "f_page_key" not in _table_xcolumns(conn, "entities"):
            conn.execute(
                "ALTER TABLE entities ADD COLUMN f_page_key TEXT "
                "GENERATED ALWAYS AS (json_extract(data_json, '$.page_key')) VIRTUAL"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                claim_text TEXT,
                status TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        # The three json paths this table is queried by, as generated columns: equal to the json by
        # construction.  ``status`` is deliberately absent -- ``claims.status`` is already a real
        # column and nothing filters the json spelling.
        for column, path in (("f_claim_type", "$.claim_type"), ("f_page_key", "$.locator.page_key"),
                             ("f_source_page", "$.source_page")):
            if column not in _table_xcolumns(conn, "claims"):
                conn.execute(
                    f"ALTER TABLE claims ADD COLUMN {column} TEXT "
                    f"GENERATED ALWAYS AS (json_extract(data_json, '{path}')) VIRTUAL"
                )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        if "f_page_key" not in _table_xcolumns(conn, "evidence"):
            conn.execute(
                "ALTER TABLE evidence ADD COLUMN f_page_key TEXT "
                "GENERATED ALWAYS AS (json_extract(data_json, '$.locator.page_key')) VIRTUAL"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_sets (
                change_set_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        for column, path in (("f_status", "$.status"), ("f_idempotency_key", "$.idempotency_key")):
            if column not in _table_xcolumns(conn, "change_sets"):
                conn.execute(
                    f"ALTER TABLE change_sets ADD COLUMN {column} TEXT "
                    f"GENERATED ALWAYS AS (json_extract(data_json, '{path}')) VIRTUAL"
                )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_set_idempotency (
                idempotency_key TEXT PRIMARY KEY,
                change_set_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS governance_queue (
                item_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mutation_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                mutation_type TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        outbox_columns = [
            ("payload_text", "TEXT"),
            ("attempt_count", "INTEGER DEFAULT 0"),
            ("last_error", "TEXT"),
            ("available_at", "TEXT"),
            ("started_at", "TEXT"),
            ("completed_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("idempotency_key", "TEXT"),
            ("validation_mode", "TEXT DEFAULT 'full'"),
        ]
        for column_name, column_type in outbox_columns:
            try:
                conn.execute(f"ALTER TABLE mutation_outbox ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            "UPDATE mutation_outbox SET available_at = created_at "
            "WHERE available_at IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_ready "
            "ON mutation_outbox(status, available_at, lease_until, id)"
        )
        # The unique idempotency index is defence-in-depth on top of the SELECT
        # performed by enqueue_mutation().  A database written by an earlier
        # release can already hold duplicate keys, and failing here would make
        # init_db() - and therefore every command - unusable on that database.
        # ``_ensure_idempotency_index`` degrades to a partial index over the
        # non-terminal rows rather than giving up the guarantee entirely.
        _ensure_idempotency_index(conn, "mutation_outbox", "idx_mutation_outbox_idempotency")
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_search_index USING fts5(
                node_key, title, summary, text,
                tokenize='unicode61 remove_diacritics 1'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_search_index_state (
                node_key TEXT PRIMARY KEY,
                content_hash TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alias_registry (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operational_memory (
                memory_id TEXT PRIMARY KEY,
                memory_type TEXT,
                score REAL,
                status TEXT,
                ttl REAL,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE operational_memory ADD COLUMN status TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE operational_memory ADD COLUMN ttl REAL")
        except sqlite3.OperationalError:
            pass
        # ``memory_key`` and ``source_claim_id`` have no real column and are queried (the composite
        # one by ``WHERE memory_type = ? AND memory_key = ?``), so they get *generated* columns:
        # equal to the json by construction, so there is no drift to compare.  The
        # ``memory_type``/``status`` json indexes are a different case -- every statement names the
        # real columns, which an expression index cannot serve -- so those are dropped rather than
        # joined by a third spelling.
        for column, path in (("f_memory_key", "$.memory_key"), ("f_source_claim_id", "$.source_claim_id")):
            if column not in _table_xcolumns(conn, "operational_memory"):
                conn.execute(
                    f"ALTER TABLE operational_memory ADD COLUMN {column} TEXT "
                    f"GENERATED ALWAYS AS (json_extract(data_json, '{path}')) VIRTUAL"
                )
        _create_operational_memory_index(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_graph_edges (
                source_id TEXT,
                target_id TEXT,
                relation TEXT,
                weight REAL,
                updated_at TEXT,
                PRIMARY KEY (source_id, target_id, relation)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline_events (
                id TEXT PRIMARY KEY,
                event_date TEXT,
                action TEXT,
                sentiment TEXT,
                description TEXT,
                entity_id TEXT,
                entity_title TEXT,
                source_file TEXT,
                extracted_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_date ON timeline_events(event_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_entity ON timeline_events(entity_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                filepath TEXT PRIMARY KEY,
                file_hash TEXT,
                processed_at TEXT,
                observed_mtime_ns INTEGER,
                observed_size INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                model TEXT NOT NULL,
                candidates INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0,
                failed_batches INTEGER DEFAULT 0,
                last_error TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_runs_status "
            "ON embedding_runs(status, updated_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_rate_reservations (
                reservation_id TEXT PRIMARY KEY,
                reserved_at REAL NOT NULL,
                token_count INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_rate_window "
            "ON embedding_rate_reservations(reserved_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                task_type TEXT,
                payload TEXT,
                status TEXT,
                retries INTEGER DEFAULT 0,
                error_msg TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        for column_name, column_type in [
            ("available_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("lease_owner", "TEXT"),
            ("lease_token", "TEXT"),
            ("lease_generation", "INTEGER DEFAULT 0"),
            ("idempotency_key", "TEXT"),
            ("task_packet_path", "TEXT"),
            ("completed_at", "TEXT"),
            ("result_json", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute("UPDATE jobs SET available_at = created_at WHERE available_at IS NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_ready "
            "ON jobs(status, available_at, lease_until, created_at)"
        )
        # Same defence-in-depth trade as idx_mutation_outbox_idempotency above:
        # a database written by a release that predates this index can already
        # hold duplicate keys, and failing here would make init_db() - and
        # therefore every command in a fresh process - unusable.
        _ensure_idempotency_index(conn, "jobs", "idx_jobs_idempotency")
        
        # Add expression-based indexes for performance
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_f_page_key ON entities (f_page_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_f_page_key ON claims (f_page_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_f_source_page ON claims (f_source_page)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_f_page_key ON evidence (f_page_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_change_sets_f_status ON change_sets (f_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_change_sets_f_idempotency ON change_sets (f_idempotency_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_om_f_memory_key ON operational_memory (memory_type, f_memory_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_om_f_source_claim ON operational_memory (f_source_claim_id)")
            # ``timeline_projection_parity`` runs on every timeline query; the index that serves the
            # claim_type predicate is on the generated column now, so the predicate no longer has to
            # match the expression text verbatim (its predecessor was an expression index and did).
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_f_claim_type ON claims (f_claim_type)")
        except sqlite3.OperationalError as e:
            # Older SQLite versions might not support expression indexes
            import logging
            logging.getLogger("vector-lake-db").warning(f"Could not create JSON expression indexes: {e}")
    _INITIALIZED_DB_PATHS.add(db_key)



def search_index_keys() -> set[str]:
    """Every node_key currently materialised in the FTS projection."""
    init_db()
    return {
        row["node_key"]
        for row in get_connection().execute("SELECT DISTINCT node_key FROM wiki_search_index")
    }


def upsert_search_index(node_key: str, title: str, summary: str, text: str, content_hash: str | None = None):
    """Write one pre-tokenized node into the FTS projection.

    Callers must pass text that is already tokenized; re-tokenizing here doubled
    the jieba cost of every index rebuild.
    """
    conn = get_connection()
    with transaction():
        # FTS5 doesn't support ON CONFLICT REPLACE directly, so we delete then insert.
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("""
            INSERT INTO wiki_search_index (node_key, title, summary, text)
            VALUES (?, ?, ?, ?)
        """, (node_key, title, summary, text))
        if content_hash is not None:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_search_index_state (node_key, content_hash, updated_at) VALUES (?, ?, ?)",
                (node_key, content_hash, datetime.now(timezone.utc).isoformat()),
            )


def clear_search_index():
    """Reset the FTS projection and its content-hash state."""
    init_db()
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM wiki_search_index")
        conn.execute("DELETE FROM wiki_search_index_state")


def search_index_state() -> dict[str, str]:
    """node_key -> content hash of the last tokenized content."""
    init_db()
    return {
        row["node_key"]: row["content_hash"]
        for row in get_connection().execute("SELECT node_key, content_hash FROM wiki_search_index_state")
    }

def upsert_embedding(entity_id: str, embedding: list[float]):
    """Store one unit vector under a page key.

    The normalisation is load-bearing, not cosmetic: ``tool_search`` converts sqlite-vec's L2
    distance with ``1 - d^2/2`` and gates on a cosine threshold, and both are only correct for unit
    vectors.  A sampled norm measures exactly 1.000000 because of this function -- if it were
    removed, the conversion and the gate would go wrong together and quietly.
    """
    if not embedding:
        return
    import math
    norm = math.sqrt(sum(x*x for x in embedding))
    if norm > 0:
        embedding = [x/norm for x in embedding]
    conn = get_connection()
    import sqlite_vec
    query_blob = sqlite_vec.serialize_float32(embedding)
    with transaction():
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (entity_id,))
        conn.execute("INSERT INTO vec_embeddings (entity_id, embedding) VALUES (?, ?)", (entity_id, query_blob))


def delete_embedding(entity_id: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (str(entity_id),))

def delete_stale_embeddings(valid_entity_ids: set[str]) -> int:
    conn = get_connection()
    valid = {str(item) for item in valid_entity_ids if item}
    rows = conn.execute("SELECT entity_id FROM vec_embeddings").fetchall()
    stale = [row["entity_id"] for row in rows if row["entity_id"] not in valid]
    if not stale:
        return 0
    with transaction():
        conn.executemany("DELETE FROM vec_embeddings WHERE entity_id = ?", [(entity_id,) for entity_id in stale])
    return len(stale)

def count_embeddings() -> int:
    conn = get_connection()
    return int(conn.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0])


def record_embedding_projection(model: str, dimension: int) -> None:
    """Record which model produced the vectors currently in ``vec_embeddings``.

    Called by the backfill once it has written rows, because that is the only moment the
    table's content and this marker agree.  See the DDL for why the model cannot live on the
    vector rows themselves.
    """
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        get_connection().execute(
            "INSERT INTO vec_embedding_projection (id, model, dimension, written_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET model = excluded.model, "
            "dimension = excluded.dimension, written_at = excluded.written_at",
            (str(model), int(dimension), now),
        )


def embedding_projection_state() -> dict:
    """The recorded model/dimension of the stored projection, or ``{}`` when unrecorded."""
    row = get_connection().execute(
        "SELECT model, dimension, written_at FROM vec_embedding_projection WHERE id = 1"
    ).fetchone()
    return dict(row) if row else {}


def start_embedding_run(run_id: str, model: str, candidates: int):
    import os

    now = datetime.now(timezone.utc).isoformat()
    stale_after = max(60, int(os.environ.get("VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS", "3600")))
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
    with transaction():
        conn = get_connection()
        conn.execute(
            "UPDATE embedding_runs SET status = 'abandoned', completed_at = ?, updated_at = ?, "
            "last_error = 'Previous embedding process stopped without finalizing the run' "
            "WHERE status = 'running' AND updated_at < ?",
            (now, now, cutoff),
        )
        conn.execute(
            "INSERT INTO embedding_runs "
            "(run_id, status, model, candidates, processed, failed_batches, started_at, updated_at) "
            "VALUES (?, 'running', ?, ?, 0, 0, ?, ?)",
            (run_id, model, int(candidates), now, now),
        )


def update_embedding_run(run_id: str, processed: int, failed_batches: int = 0, last_error: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        get_connection().execute(
            "UPDATE embedding_runs SET processed = ?, failed_batches = ?, last_error = ?, updated_at = ? "
            "WHERE run_id = ?",
            (int(processed), int(failed_batches), str(last_error)[:2000], now, run_id),
        )


def finish_embedding_run(run_id: str, status: str, processed: int, failed_batches: int = 0, last_error: str = ""):
    if status not in {"completed", "failed", "partial"}:
        raise ValueError(f"Unsupported embedding run status: {status}")
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        get_connection().execute(
            "UPDATE embedding_runs SET status = ?, processed = ?, failed_batches = ?, "
            "last_error = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
            (status, int(processed), int(failed_batches), str(last_error)[:2000], now, now, run_id),
        )

def delete_search_index(node_key: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM wiki_search_index_state WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (node_key,))

def delete_node_cascade(node_key: str):
    conn = get_connection()
    with transaction():
        rows = conn.execute(
            "SELECT entity_id FROM entities "
            "WHERE entity_id = ? OR canonical_name = ? "
            "OR f_page_key = ?",
            (node_key, node_key, node_key),
        ).fetchall()
        entity_ids = {row["entity_id"] for row in rows}
        related_ids = sorted(entity_ids | {node_key})
        placeholders = ",".join("?" for _ in related_ids)
        old_claim_rows = conn.execute(
            "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE "
            "f_page_key = ? OR "
            "f_source_page IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        ).fetchall()

        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM wiki_search_index_state WHERE node_key = ?", (node_key,))
        conn.execute(f"DELETE FROM vec_embeddings WHERE entity_id IN ({placeholders})", related_ids)
        conn.execute(
            "DELETE FROM claims WHERE "
            "f_page_key = ? OR "
            "f_source_page IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        )
        conn.execute(
            "DELETE FROM evidence WHERE f_page_key = ?",
            (node_key,),
        )
        conn.execute(
            "DELETE FROM sources WHERE source_id = ? OR "
            "json_extract(data_json, '$.canonical_source_page') = ?",
            (node_key, node_key + ".md"),
        )
        conn.execute(f"DELETE FROM alias_registry WHERE key = ? OR value IN ({placeholders})", [node_key, *related_ids])
        conn.execute(
            f"DELETE FROM claim_graph_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            [*related_ids, *related_ids],
        )
        # Deferred on purpose, and load-order-bearing: ``tool_timeline`` imports this
        # module at module scope.  The deferred binding is also what lets
        # ``tests/test_timeline_projection.py`` inject a projection failure by patching
        # ``tool_timeline``'s name.  See ``tests/test_import_layering.py``.
        from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta

        sync_timeline_events_for_claim_delta(old_claim_rows, [])
        conn.execute(f"DELETE FROM entities WHERE entity_id IN ({placeholders})", related_ids)

    return {"page_key": node_key, "entity_ids": sorted(entity_ids)}


def published_edge_projection_drift(max_examples: int = 3) -> dict[str, object]:
    """Whether the search projection still holds the published edge set.

    The published ``weighted_edges`` in ``index.json`` is the source; ``page_index_edges`` is its read
    projection, which ``page_index_projection.adjacency`` walks.  This check used to compare that
    projection against ``page_graph_edges`` -- a *second* SQLite table holding the same pairs, whose
    only reader was this check, so it compared two projections of one source instead of the projection
    against the source.  That table is gone (nothing read it) and the comparison is now with the file.

    The published set collapses duplicate pairs to their maximum weight, so both sides are compared as
    distinct pairs.  Read-only, and exact: both sides are tens of thousands of pairs, where the
    LIMIT-probe machinery this replaced existed for a 1.29M-row legacy state that no longer occurs.
    """
    published: set[tuple[str, str]] = set()
    file_rows = 0
    read_error = ""
    index_path = get_index_path()
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Named rather than treated as an empty published set: "the projection has pairs the file
            # does not" and "the file could not be read" are different findings, and reporting the
            # second as the first sends the operator looking in the wrong place.
            data = {}
            read_error = f"{type(exc).__name__}: {exc}"
        for edge in data.get("weighted_edges") or []:
            source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
            if source and target:
                published.add((source, target))
                file_rows += 1
    conn = get_connection()
    projection = {
        (str(row[0]), str(row[1]))
        for row in conn.execute("SELECT DISTINCT source_key, target_key FROM page_index_edges")
    }
    extra = sorted(projection - published)
    missing = sorted(published - projection)
    # Multiplicity is reported separately because the comparison is over *distinct* pairs: the
    # published set collapses duplicates to their maximum weight and the projection is re-inserted
    # from that set, so a repeated row means something wrote the table outside that path.
    projection_rows_raw = int(conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0])
    return {
        "projection_rows": len(projection),
        "published_rows": len(published),
        "difference": len(projection) - len(published),
        "extra_examples": extra[:max_examples],
        "extra": bool(extra),
        "missing": bool(missing),
        "missing_example": missing[0] if missing else None,
        "duplicate_pairs": projection_rows_raw != len(projection) or file_rows != len(published),
        "published_read_error": read_error,
    }


def enqueue_mutation(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
    idempotency_key: str | None = None,
    validation_mode: str = "full",
) -> int:
    if mutation_type not in {"update", "delete"}:
        raise ValueError(f"Unsupported mutation_type: {mutation_type}")
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    init_db()
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        if idempotency_key:
            existing = conn.execute(
                "SELECT id, status FROM mutation_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["status"] == "failed":
                    conn.execute(
                        "UPDATE mutation_outbox SET status = 'pending', attempt_count = 0, "
                        "last_error = NULL, available_at = ?, lease_until = NULL WHERE id = ?",
                        (now, existing["id"]),
                    )
                return int(existing["id"])
        cursor = conn.execute(
            "INSERT INTO mutation_outbox "
            "(filename, mutation_type, payload_text, status, attempt_count, created_at, available_at, "
            "idempotency_key, validation_mode) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
            (filename, mutation_type, payload_text, now, now, idempotency_key, validation_mode),
        )
        return int(cursor.lastrowid)


def is_managed_projection_state(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
) -> bool:
    """Return whether a filesystem event matches the latest durable projection intent."""
    init_db()
    row = get_connection().execute(
        "SELECT mutation_type, payload_text FROM mutation_outbox "
        "WHERE filename = ? ORDER BY id DESC LIMIT 1",
        (str(filename),),
    ).fetchone()
    if row is None or str(row["mutation_type"]) != str(mutation_type):
        return False
    if mutation_type == "delete":
        return True
    return row["payload_text"] == payload_text


def claim_mutation_outbox(limit: int = 50, lease_seconds: int = 120) -> list[dict]:
    """Atomically claim ready rows, including abandoned processing leases."""
    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
    with transaction():
        rows = conn.execute(
            "SELECT id FROM mutation_outbox WHERE "
            "(status = 'pending' AND COALESCE(available_at, created_at, '') <= ?) OR "
            "(status = 'processing' AND COALESCE(lease_until, '') <= ?) "
            "ORDER BY id ASC LIMIT ?",
            (now, now, max(1, int(limit))),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE mutation_outbox SET status = 'processing', "
            f"attempt_count = COALESCE(attempt_count, 0) + 1, started_at = ?, "
            f"lease_until = ? WHERE id IN ({placeholders})",
            [now, lease_until, *ids],
        )
        claimed = conn.execute(
            f"SELECT * FROM mutation_outbox WHERE id IN ({placeholders}) ORDER BY id ASC",
            ids,
        ).fetchall()
        return [dict(row) for row in claimed]


def complete_mutation_outbox(outbox_id: int):
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'completed', completed_at = ?, "
            "lease_until = NULL, last_error = NULL WHERE id = ?",
            (now, outbox_id),
        )


def fail_mutation_outbox(
    outbox_id: int,
    error: str,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
) -> str:
    conn = get_connection()
    row = conn.execute(
        "SELECT attempt_count FROM mutation_outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown mutation_outbox id: {outbox_id}")
    attempts = int(row["attempt_count"] or 0)
    now_dt = datetime.now(timezone.utc)
    terminal = attempts >= max(1, int(max_attempts))
    status = "failed" if terminal else "pending"
    delay_seconds = 0.0 if terminal else max(0.0, float(backoff_base)) * (2 ** max(0, attempts - 1))
    available_at = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
    with transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = ?, last_error = ?, available_at = ?, "
            "lease_until = NULL WHERE id = ?",
            (status, str(error)[:4000], available_at, outbox_id),
        )
    return status

def search_wiki(query: str, limit: int = 50) -> list[dict]:
    import re
    from vector_lake import tokenizer as _tokenizer

    query = re.sub(r'[^\w\s\u4e00-\u9fa5]', ' ', query) if query else ""
    query_tok = _tokenizer.tokenize_joined(query)

    query_tok = query_tok.strip()
    if not query_tok:
        return []
        
    query_esc = " ".join(f'"{t}"' for t in query_tok.split())

    conn = get_connection()
    cur = conn.execute("""
        SELECT node_key, title, summary, bm25(wiki_search_index) as rank 
        FROM wiki_search_index 
        WHERE wiki_search_index MATCH ? 
        ORDER BY rank LIMIT ?
    """, (query_esc, limit))
    return [dict(row) for row in cur.fetchall()]

def get_processed_files() -> dict[str, str]:
    conn = get_connection()
    cur = conn.execute("SELECT filepath, file_hash FROM processed_files")
    return {row["filepath"]: row["file_hash"] for row in cur.fetchall()}

def mark_file_processed(
    filepath: str,
    file_hash: str,
    *,
    mtime_ns: int | None = None,
    size: int | None = None,
):
    """Record a raw source as processed, with the observation snapshot when supplied.

    The snapshot is what lets the scan skip an unchanged file without hashing it.  It is
    optional so the callers that legitimately cannot stat the file (and the contract tests)
    keep working; ``COALESCE`` then keeps any snapshot already on the row instead of clearing
    it on a write that carries none.
    """
    from datetime import datetime, timezone
    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute("""
            INSERT INTO processed_files (
                filepath, file_hash, processed_at, observed_mtime_ns, observed_size
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                file_hash = excluded.file_hash,
                processed_at = excluded.processed_at,
                observed_mtime_ns = COALESCE(excluded.observed_mtime_ns, processed_files.observed_mtime_ns),
                observed_size = COALESCE(excluded.observed_size, processed_files.observed_size)
        """, (filepath, file_hash, now_str, mtime_ns, size))

#: Statuses from which a job's work is genuinely finished.
#:
#: They are the ones ``enqueue_job`` may deduplicate against.  A job that ended ``failed`` or
#: ``cancelled`` did *not* do the work, and deduplicating against it is a dead end: the raw
#: source is still un-ingested, but every later scan gets the old terminal job id back instead of
#: a dispatchable one.  Measured on 2026-09-19, this is what left 11 sources (8 of them transient
#: "Canonical version conflict" failures) permanently un-ingestable.
INGEST_JOB_TERMINAL_SUCCESS = ("finalized", "completed")


#: Statuses that mean the job gave up rather than finished or being in progress.
#:
#: This is what ``replace_terminal`` may supersede.  The first version asked "is it *not*
#: finished", which is also true of ``queued``, ``awaiting_subagent`` and
#: ``subagent_processing`` -- so a scan superseded jobs that were still in flight.  Measured on
#: the live corpus 2026-09-19 10:50: six sources ended up with **three to four concurrent jobs
#: each**, i.e. three to four model calls for one file.
INGEST_JOB_TERMINAL_FAILURE = ("failed", "cancelled")


def _job_is_terminal_unsuccessful(conn, job_id: str) -> bool:
    row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return False
    return str(row["status"]) in INGEST_JOB_TERMINAL_FAILURE


def enqueue_job(
    task_type: str,
    payload: dict,
    idempotency_key: str | None = None,
    replace_terminal: bool = False,
    supersede_finished: bool = False,
) -> str:
    """Enqueue a job, deduplicating against an *unfinished* job with the same key.

    ``replace_terminal`` (used by the ingest scan) lets a new job supersede one that ended
    ``failed`` or ``cancelled``: the source still needs ingesting, and returning the dead job's id
    would leave it that way forever.  The superseded row is marked so the history stays readable
    instead of being deleted.

    ``supersede_finished`` extends that to a ``finalized``/``completed`` job, which is only safe
    for a caller that has *established the source is not published* -- the ingest scan does, via
    ``raw_is_published``, before it gets here.  It exists because a source can hold a finished job
    while having neither a page nor a ``processed_files`` row (measured:
    ``DHWB-20260913.md``, finalized 2026-09-17, no page anywhere in the wiki).  Without this the
    scan's enqueue was a silent no-op against the finished job's key: the file was reported
    "enqueued", marked in-flight, never dispatched, and released again by the next sweep -- a
    12-minute loop that produced nothing.  The collision is logged, because it means the page and
    the ledger disagree and an operator should know.
    """
    if supersede_finished and not replace_terminal:
        # A finished job is also "not unfinished"; keep the two switches from being used as if
        # they were independent when they are nested.
        replace_terminal = True
    import uuid
    from datetime import datetime, timezone
    init_db()
    conn = get_connection()
    job_id = uuid.uuid4().hex
    now_str = datetime.now(timezone.utc).isoformat()
    key = idempotency_key or _job_idempotency_key(task_type, payload)
    with transaction():
        if key:
            existing = conn.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing:
                existing_id = str(existing["job_id"])
                status_row = conn.execute(
                    "SELECT status FROM jobs WHERE job_id = ?", (existing_id,)
                ).fetchone()
                status = str(status_row["status"]) if status_row else ""
                unfinished_mismatch = replace_terminal and _job_is_terminal_unsuccessful(conn, existing_id)
                finished_collision = supersede_finished and status in INGEST_JOB_TERMINAL_SUCCESS
                if not (unfinished_mismatch or finished_collision):
                    return existing_id
                if finished_collision:
                    logging.getLogger("vector-lake-db").warning(
                        "Superseding %s job %s for %s: the source is not published but the job "
                        "table says the work finished, so the page and the ledger disagree.",
                        status, existing_id, (payload or {}).get("filepath"),
                    )
                # Keep the old row as history, but take its key so the new row can hold it.
                #
                # ``unfinished_mismatch`` also has to *stop the old row being dispatched*:
                # clearing the key alone left it ``failed`` with its retries unspent, and
                # ``claim_pending_jobs`` claims ``failed`` while ``retries < MAX_INGEST_ATTEMPTS``
                # -- so the superseded job and its replacement were both handed out, i.e. two
                # model calls for one source.  That is the defect this supersede path was added to
                # remove (independent review, 2026-09-19).  A *finished* row is already
                # unclaimable, so its status is left as the record of the work.
                conn.execute(
                    "UPDATE jobs SET idempotency_key = NULL, "
                    "status = CASE WHEN ? THEN 'superseded' ELSE status END, "
                    "error_msg = COALESCE(NULLIF(error_msg, ''), '') || "
                    "' [superseded by a fresh dispatch]', updated_at = ? WHERE job_id = ?",
                    (1 if unfinished_mismatch else 0, now_str, existing_id),
                )
        conn.execute("""
            INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at, available_at, idempotency_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, task_type, json.dumps(payload, ensure_ascii=False), "queued", now_str, now_str, now_str, key))
    return job_id


#: Attempts a single ingest job may consume before it becomes terminal.
#:
#: One owner for the number: ``claim_pending_jobs`` refuses to dispatch a job at the
#: cap, and ``tool_ingest.record_ingest_failure`` reports against the same value.  The
#: two disagreed before -- the runner's failure branches never consumed the budget at
#: all, so a deterministically rejecting task was re-claimed every hour forever.
MAX_INGEST_ATTEMPTS = 3


def claim_pending_jobs(limit: int = 10, lease_seconds: int = 300) -> list[dict]:
    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
    with transaction():
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE "
            "((status IN ('queued', 'failed') AND retries < ? AND COALESCE(available_at, created_at, '') <= ?) "
            "OR (status = 'dispatched' AND COALESCE(lease_until, '') <= ?)) "
            "ORDER BY created_at ASC LIMIT ?",
            (MAX_INGEST_ATTEMPTS, now, now, max(1, int(limit))),
        ).fetchall()
        job_ids = [row["job_id"] for row in rows]
        if not job_ids:
            return []
        placeholders = ",".join("?" for _ in job_ids)
        conn.execute(
            f"UPDATE jobs SET status = 'dispatched', lease_until = ?, updated_at = ? "
            f"WHERE job_id IN ({placeholders})",
            [lease_until, now, *job_ids],
        )
        claimed = conn.execute(
            f"SELECT * FROM jobs WHERE job_id IN ({placeholders}) ORDER BY created_at ASC",
            job_ids,
        ).fetchall()
        return [dict(row) for row in claimed]

def get_pending_jobs(limit: int = 10) -> list[dict]:
    conn = get_connection()
    cur = conn.execute("""
        SELECT * FROM jobs 
        WHERE status = 'queued' OR (status = 'failed' AND retries < 3)
        ORDER BY created_at ASC LIMIT ?
    """, (limit,))
    return [dict(row) for row in cur.fetchall()]

def get_jobs_by_status(statuses: list[str], limit: int = 20) -> list[dict]:
    if not statuses:
        return []
    init_db()
    conn = get_connection()
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",
        [*statuses, max(1, int(limit))],
    ).fetchall()
    return [dict(row) for row in rows]

def mark_job_awaiting_subagent(job_id: str, task_packet_path: str):
    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent', task_packet_path = ?, "
            "error_msg = ?, updated_at = ?, lease_until = NULL, lease_owner = NULL, "
            "lease_token = NULL WHERE job_id = ?",
            (task_packet_path, f"Subagent task packet: {task_packet_path}", now_str, job_id),
        )


def claim_subagent_jobs(
    limit: int = 10,
    lease_seconds: int = 3600,
    lease_owner: str | None = None,
) -> list[dict]:
    """Lease ingest tasks to one host subagent consumer at a time."""
    import os
    import secrets
    import socket

    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    owner = str(
        lease_owner
        or os.environ.get("VECTOR_LAKE_SUBAGENT_RUN_ID")
        or f"{socket.gethostname()}:{os.getpid()}"
    )
    with transaction():
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE task_type = 'ingest' AND "
            "(status = 'awaiting_subagent' OR "
            "(status = 'subagent_processing' AND COALESCE(lease_until, '') <= ?)) "
            "ORDER BY created_at ASC LIMIT ?",
            (now, max(1, int(limit))),
        ).fetchall()
        job_ids = [str(row["job_id"]) for row in rows]
        if not job_ids:
            return []
        claimed = []
        for job_id in job_ids:
            lease_token = secrets.token_urlsafe(32)
            cursor = conn.execute(
                "UPDATE jobs SET status = 'subagent_processing', lease_until = ?, "
                "lease_owner = ?, lease_token = ?, "
                "lease_generation = COALESCE(lease_generation, 0) + 1, updated_at = ? "
                "WHERE job_id = ? AND (status = 'awaiting_subagent' OR "
                "(status = 'subagent_processing' AND COALESCE(lease_until, '') <= ?))",
                (lease_until, owner, lease_token, now, job_id, now),
            )
            if cursor.rowcount != 1:
                continue
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is not None:
                claimed.append(dict(row))
        return claimed


def validate_ingest_job_finalization(job_id: str, processed_data: dict) -> dict:
    """Bind finalization to the exact leased job payload."""
    init_db()
    row = get_connection().execute(
        "SELECT * FROM jobs WHERE job_id = ?",
        (str(job_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown ingest job: {job_id}")
    if row["task_type"] != "ingest":
        raise ValueError(f"Job {job_id} is not an ingest job")
    if row["status"] != "subagent_processing":
        raise ValueError(f"Job {job_id} cannot be finalized from status {row['status']}")
    lease_token = str(processed_data.get("lease_token") or "")
    expected_token = str(row["lease_token"] or "")
    if not lease_token or lease_token != expected_token:
        raise ValueError(f"Job {job_id} lease_token does not match the current lease")
    lease_owner = str(processed_data.get("lease_owner") or "")
    expected_owner = str(row["lease_owner"] or "")
    if not lease_owner or lease_owner != expected_owner:
        raise ValueError(f"Job {job_id} lease_owner does not match the current lease")
    try:
        lease_generation = int(processed_data.get("lease_generation"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Job {job_id} requires a valid lease_generation") from exc
    if lease_generation != int(row["lease_generation"] or 0):
        raise ValueError(f"Job {job_id} lease_generation does not match the current lease")
    lease_until = datetime.fromisoformat(str(row["lease_until"] or "").replace("Z", "+00:00"))
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    if lease_until <= datetime.now(timezone.utc):
        raise ValueError(f"Job {job_id} lease has expired")
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Job {job_id} has an invalid payload") from exc
    for key in ("filepath", "hash"):
        if str(processed_data.get(key) or "") != str(payload.get(key) or ""):
            raise ValueError(f"Job {job_id} {key} does not match its queued payload")
    expected_name = str(payload.get("canonical_name") or "")
    supplied_name = str(processed_data.get("canonical_name") or "")
    if expected_name and supplied_name != expected_name:
        raise ValueError(f"Job {job_id} canonical_name does not match its queued payload")
    expected_source_hash = str(payload.get("source_hash") or "")
    supplied_source_hash = str(processed_data.get("source_hash") or "")
    if supplied_source_hash != expected_source_hash:
        raise ValueError(f"Job {job_id} source_hash does not match its queued payload")
    result = dict(row)
    result["parsed_payload"] = payload
    return result


def finalize_ingest_job(
    job_id: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    result_data: dict | None = None,
):
    """Mark a validated subagent job complete inside the caller's transaction."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    result_json = json.dumps(result_data or {}, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        "UPDATE jobs SET status = 'finalized', completed_at = ?, updated_at = ?, "
        "lease_until = NULL, lease_owner = NULL, lease_token = NULL, error_msg = '', result_json = ? "
        "WHERE job_id = ? AND status = 'subagent_processing' "
        "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? AND lease_until > ?",
        (
            now,
            now,
            result_json,
            str(job_id),
            str(lease_owner),
            str(lease_token),
            int(lease_generation),
            now,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"Ingest job {job_id} is no longer finalizable")

def expire_stale_subagent_jobs(max_age_seconds: int = 86400) -> int:
    init_db()
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, int(max_age_seconds)))).isoformat()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        cursor = conn.execute(
            "UPDATE jobs SET status = 'failed', retries = retries + 1, "
            "error_msg = 'Subagent task packet expired before finalization', updated_at = ?, "
            "available_at = ?, lease_until = NULL, lease_owner = NULL, lease_token = NULL "
            "WHERE status = 'awaiting_subagent' AND updated_at < ?",
            (now_str, now_str, cutoff),
        )
        return int(cursor.rowcount or 0)

def record_abandoned_source(filepath: str, file_hash: str, reason: str) -> int:
    """Stop dispatching this exact source content after it kept failing deterministically.

    Keyed on ``(filepath, file_hash)`` on purpose: editing the source changes its hash, so a
    corrected file is dispatchable again without any operator action, while the exact bytes that
    keep failing stop consuming model calls.  Only deterministic failures land here -- a
    transient one is released for retry instead (see ``release_job_for_retry``).

    Returns the number of terminal jobs recorded for this content.
    """
    from datetime import datetime, timezone

    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute(
            "INSERT INTO ingest_abandoned_sources (filepath, file_hash, reason, terminal_jobs, abandoned_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(filepath, file_hash) DO UPDATE SET "
            "reason = excluded.reason, terminal_jobs = terminal_jobs + 1, abandoned_at = excluded.abandoned_at",
            (str(filepath), str(file_hash), str(reason)[:500], now),
        )
        row = conn.execute(
            "SELECT terminal_jobs FROM ingest_abandoned_sources WHERE filepath = ? AND file_hash = ?",
            (str(filepath), str(file_hash)),
        ).fetchone()
    return int(row["terminal_jobs"]) if row else 1


def abandoned_source_keys() -> set[tuple[str, str]]:
    """``{(filepath, file_hash), ...}`` of sources that must not be re-dispatched."""
    try:
        return {
            (str(row[0]), str(row[1]))
            for row in get_connection().execute(
                "SELECT filepath, file_hash FROM ingest_abandoned_sources"
            )
        }
    except sqlite3.OperationalError:
        # A database predating the table simply has no abandoned sources.
        return set()


def list_abandoned_sources() -> list[dict]:
    """Abandoned sources, newest first, for an operator to inspect or clear."""
    try:
        return [
            dict(row)
            for row in get_connection().execute(
                "SELECT filepath, file_hash, reason, terminal_jobs, abandoned_at "
                "FROM ingest_abandoned_sources ORDER BY abandoned_at DESC"
            )
        ]
    except sqlite3.OperationalError:
        return []


def list_terminal_failed_jobs() -> list[dict]:
    """Jobs that reached the attempt budget, newest first, with the source each names."""
    try:
        rows = get_connection().execute(
            "SELECT job_id, task_type, retries, updated_at, error_msg, payload FROM jobs "
            "WHERE status = 'failed' AND retries >= ? ORDER BY updated_at DESC",
            (MAX_INGEST_ATTEMPTS,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    jobs = []
    for row in rows:
        try:
            filepath = json.loads(row["payload"] or "{}").get("filepath", "")
        except (TypeError, ValueError):
            filepath = ""
        jobs.append(
            {
                "job_id": str(row["job_id"]),
                "task_type": str(row["task_type"]),
                "retries": int(row["retries"] or 0),
                "updated_at": str(row["updated_at"]),
                "error_msg": str(row["error_msg"] or ""),
                "filepath": str(filepath),
            }
        )
    return jobs


def close_terminal_failed_jobs(source_ingested_only: bool = True) -> dict:
    """Mark terminal-failed jobs ``superseded`` once their source is genuinely ingested.

    These are the jobs that spent their attempt budget, and they are what makes ``doctor`` report
    ``[FAIL] Ingest Jobs: terminal_failed:N``.  A job whose source has since been ingested (a
    ledger row) is finished business recorded twice: the *work* is done and the failure is
    history.  Leaving them ``failed`` keeps a FAIL on the health surface that no longer describes
    anything actionable -- measured on 2026-09-19: 11 jobs, every one of whose sources was in the
    ledger.

    ``source_ingested_only`` refuses to close a job whose source has no ledger row, because that
    one really is unfinished work and the abandonment mechanism is what decides its fate.
    """
    conn = get_connection()
    ledger = {str(row[0]) for row in conn.execute("SELECT filepath FROM processed_files")}
    closed: list[str] = []
    kept: list[str] = []
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        for job in list_terminal_failed_jobs():
            filepath = job["filepath"]
            if source_ingested_only and (not filepath or filepath not in ledger):
                kept.append(job["job_id"])
                continue
            conn.execute(
                "UPDATE jobs SET status = 'superseded', updated_at = ?, "
                "error_msg = error_msg || ' [closed: the source was ingested by a later job]' "
                "WHERE job_id = ? AND status = 'failed'",
                (now, job["job_id"]),
            )
            closed.append(job["job_id"])
    return {"closed": closed, "kept": kept}


def clear_abandoned_sources(filepath: str | None = None) -> int:
    """Allow abandoned source(s) to be dispatched again; returns how many were cleared."""
    conn = get_connection()
    with transaction():
        if filepath:
            cursor = conn.execute(
                "DELETE FROM ingest_abandoned_sources WHERE filepath = ?", (str(filepath),)
            )
        else:
            cursor = conn.execute("DELETE FROM ingest_abandoned_sources")
    return int(cursor.rowcount or 0)


def release_job_for_retry(job_id: str, reason: str) -> None:
    """Make a job dispatchable again *without* spending its terminal attempt budget.

    The budget exists to stop a deterministic rejection being retried forever (a schema
    violation, a name that no gate will ever accept).  A transient rejection is a different
    thing: ``Canonical version conflict`` means the page moved between the model reading it and
    ``finalize_ingest`` writing it, and the next attempt reads the new version.  Counting those
    against the cap turned 8 transient conflicts on the live corpus into permanently
    un-ingested sources -- terminal, and (before ``enqueue_job`` grew ``replace_terminal``)
    impossible to re-enqueue because the dead job held the idempotency key.

    ``retries`` is left alone, so ``claim_pending_jobs`` keeps dispatching while the budget
    lasts; only the lease is released.
    """
    from datetime import datetime, timezone

    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    immediate = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    with transaction():
        conn.execute(
            "UPDATE jobs SET status = 'failed', error_msg = ?, updated_at = ?, available_at = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL WHERE job_id = ?",
            (f"{str(reason)[:460]} [transient, retry does not spend the attempt budget]", now_str, immediate, job_id),
        )


def update_job_status(job_id: str, status: str, error_msg: str = ""):
    from datetime import datetime, timezone
    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        if status == "failed":
            row = conn.execute("SELECT retries FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            next_retry = int(row["retries"] or 0) + 1 if row else 1
            available_at = (datetime.now(timezone.utc) + timedelta(seconds=5 * (2 ** max(0, next_retry - 1)))).isoformat()
            conn.execute("""
                UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?, retries = retries + 1,
                    available_at = ?, lease_until = NULL, lease_owner = NULL, lease_token = NULL
                WHERE job_id = ?
            """, (status, error_msg, now_str, available_at, job_id))
        else:
            completed_at = now_str if status in {"finalized", "completed"} else None
            conn.execute("""
                UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?, lease_until = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    completed_at = COALESCE(?, completed_at)
                WHERE job_id = ?
            """, (status, error_msg, now_str, completed_at, job_id))



def backup_database(destination_path: str | Path | None = None):
    """Create a transactionally consistent SQLite backup of the active database."""
    import time

    if not get_db_path().exists():
        init_db()
    if destination_path is None:
        backup_dir = get_meta_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"vector_lake_{int(time.time())}.db.bak"
    else:
        backup_path = Path(destination_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(str(backup_path))
    try:
        get_connection().backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
    finally:
        destination.close()
    return str(backup_path)
