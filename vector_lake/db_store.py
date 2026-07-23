import atexit
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from vector_lake.wiki_utils import get_meta_dir, normalize_semantic_text
import sqlite_vec

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALIZED_DB_PATHS: set[str] = set()
_CONNECTIONS_LOCK = threading.RLock()
_CONNECTIONS: dict[int, sqlite3.Connection] = {}


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

def get_connection() -> sqlite3.Connection:
    db_path = get_db_path().resolve()
    db_key = str(db_path)
    conn = getattr(_LOCAL, "conn", None)
    with _CONNECTIONS_LOCK:
        tracked = conn is not None and id(conn) in _CONNECTIONS
    if conn is not None and (
        getattr(_LOCAL, "db_key", None) != db_key or not tracked
    ):
        if tracked:
            close_connection()
        else:
            _LOCAL.conn = None
            _LOCAL.db_key = None
        conn = None
    if conn is None:
        conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        
        # Load sqlite-vec extension
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        
        _LOCAL.conn = conn
        _LOCAL.db_key = db_key
        with _CONNECTIONS_LOCK:
            _CONNECTIONS[id(conn)] = conn
    return conn

def close_connection():
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        with _CONNECTIONS_LOCK:
            _CONNECTIONS.pop(id(conn), None)
        conn.close()
        _LOCAL.conn = None
        _LOCAL.db_key = None
    _LOCAL.in_transaction = False


def close_all_connections() -> None:
    """Close tracked SQLite handles, including handles owned by worker threads."""
    with _CONNECTIONS_LOCK:
        connections = list(_CONNECTIONS.values())
        _CONNECTIONS.clear()
    for conn in connections:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    local_conn = getattr(_LOCAL, "conn", None)
    if local_conn is not None and id(local_conn) not in _CONNECTIONS:
        _LOCAL.conn = None
        _LOCAL.db_key = None
    _LOCAL.in_transaction = False


atexit.register(close_all_connections)

@contextmanager
def transaction():
    conn = get_connection()
    in_tx = getattr(_LOCAL, 'in_transaction', False)
    if in_tx:
        yield conn
    else:
        max_retries = 60
        for attempt in range(max_retries):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    import time
                    import random
                    time.sleep(0.5 + random.random())
                    continue
                raise
        _LOCAL.in_transaction = True
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            _LOCAL.in_transaction = False

_INIT_DB_DONE = False
_INIT_LOCK = threading.Lock()

def init_db():
    db_path = get_db_path()
    db_key = str(db_path.resolve())
    if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
        return
    with _INIT_LOCK:
        if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
            return
        _INITIALIZED_DB_PATHS.discard(db_key)
        _init_db_once(db_key)


def _init_db_once(db_key: str):
    conn = get_connection()
    try:
        in_tx = getattr(_LOCAL, 'in_transaction', False)
        if not in_tx:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA foreign_keys=ON")
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
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                entity_id TEXT PRIMARY KEY,
                embedding float[3072]
            )
        """)
        for col, col_type in [("type", "TEXT"), ("status", "TEXT"), ("ttl", "INTEGER"), ("decay_weight", "REAL")]:
            try:
                conn.execute(f"ALTER TABLE entities ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                claim_text TEXT,
                status TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_artifacts (
                artifact_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                sha256 TEXT,
                byte_size INTEGER,
                mime_type TEXT,
                storage_uri TEXT,
                integrity_status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_artifacts_source "
            "ON source_artifacts(source_id, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extraction_runs (
                run_id TEXT PRIMARY KEY,
                page_key TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                extractor_name TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_extraction_runs_page "
            "ON extraction_runs(page_key, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_versions (
                claim_version_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                claim_family_id TEXT NOT NULL,
                page_key TEXT,
                version_no INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(claim_family_id, record_hash)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_versions_family "
            "ON claim_versions(claim_family_id, version_no)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_versions (
                evidence_version_id TEXT PRIMARY KEY,
                evidence_id TEXT NOT NULL,
                evidence_family_id TEXT NOT NULL,
                page_key TEXT,
                version_no INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(evidence_family_id, record_hash)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evidence_versions_family "
            "ON evidence_versions(evidence_family_id, version_no)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_identities (
                entity_id TEXT PRIMARY KEY,
                page_key TEXT NOT NULL UNIQUE,
                canonical_name TEXT,
                identity_origin TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_assessments (
                assessment_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                assessment_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                method_version TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_assessments_claim "
            "ON claim_assessments(claim_id, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS critical_decision_registry (
                decision_id TEXT PRIMARY KEY,
                registry_version TEXT NOT NULL,
                status TEXT NOT NULL,
                risk_weight REAL NOT NULL,
                verification TEXT NOT NULL,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_registry (
                schema_id TEXT NOT NULL,
                version TEXT NOT NULL,
                dialect_id TEXT NOT NULL,
                status TEXT NOT NULL,
                schema_hash TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (schema_id, version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quality_evaluation_runs (
                evaluation_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                dataset_version TEXT NOT NULL,
                evaluator_version TEXT NOT NULL,
                status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quality_runs_dataset "
            "ON quality_evaluation_runs(dataset_id, dataset_version, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_sets (
                change_set_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
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
            CREATE TABLE IF NOT EXISTS merge_journal (
                journal_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_merge_journal_item "
            "ON merge_journal(item_id, updated_at)"
        )
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
            ("lease_owner", "TEXT"),
            ("lease_token", "TEXT"),
            ("lease_generation", "INTEGER DEFAULT 0"),
            ("superseded_by", "INTEGER"),
            ("idempotency_key", "TEXT"),
            ("validation_mode", "TEXT DEFAULT 'full'"),
            ("base_version", "TEXT"),
            ("projection_base_hash", "TEXT"),
        ]
        for column_name, column_type in outbox_columns:
            try:
                conn.execute(f"ALTER TABLE mutation_outbox ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_filename_status "
            "ON mutation_outbox(filename, status, id DESC)"
        )
        conn.execute(
            "UPDATE mutation_outbox SET available_at = created_at "
            "WHERE available_at IS NULL"
        )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE mutation_outbox SET status = 'superseded', "
            "superseded_by = ("
            "  SELECT MAX(newer.id) FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            "), completed_at = COALESCE(completed_at, ?), lease_until = NULL, "
            "lease_owner = NULL, lease_token = NULL "
            "WHERE status IN ('pending', 'processing') AND EXISTS ("
            "  SELECT 1 FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            ")",
            (now,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_ready "
            "ON mutation_outbox(status, available_at, lease_until, id)"
        )
        conn.execute("DROP INDEX IF EXISTS idx_mutation_outbox_idempotency")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_idempotency_lookup "
            "ON mutation_outbox(idempotency_key, id DESC) WHERE idempotency_key IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_filename_status "
            "ON mutation_outbox(filename, status, id DESC)"
        )
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_search_index USING fts5(
                node_key, title, summary, text,
                tokenize='unicode61 remove_diacritics 1'
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_graph_nodes (
                node_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
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
            CREATE TABLE IF NOT EXISTS page_graph_edges (
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
                processed_at TEXT
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
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
            "ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        
        # Add expression-based indexes for performance
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (json_extract(data_json, '$.type'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_status ON entities (json_extract(data_json, '$.status'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_page_key ON entities (json_extract(data_json, '$.page_key'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_page_key ON claims (json_extract(data_json, '$.locator.page_key'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_page_key ON evidence (json_extract(data_json, '$.locator.page_key'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON operational_memory (json_extract(data_json, '$.memory_type'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON operational_memory (json_extract(data_json, '$.status'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_source_claim ON operational_memory (json_extract(data_json, '$.source_claim_id'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_key ON operational_memory (memory_type, json_extract(data_json, '$.memory_key'))")
        except sqlite3.OperationalError as e:
            # Older SQLite versions might not support expression indexes
            import logging
            logging.getLogger("vector-lake-db").warning(f"Could not create JSON expression indexes: {e}")
    _INITIALIZED_DB_PATHS.add(db_key)



def upsert_search_index(node_key: str, title: str, summary: str, text: str):
    try:
        from vector_lake.tokenizer_runtime import tokenize_for_fts
        title_tok = tokenize_for_fts(title)
        summary_tok = tokenize_for_fts(summary)
        text_tok = tokenize_for_fts(text)
    except ImportError:
        title_tok = title if title else ""
        summary_tok = summary if summary else ""
        text_tok = text if text else ""

    conn = get_connection()
    with transaction():
        # FTS5 doesn't support ON CONFLICT REPLACE directly, so we delete then insert.
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("""
            INSERT INTO wiki_search_index (node_key, title, summary, text)
            VALUES (?, ?, ?, ?)
        """, (node_key, title_tok, summary_tok, text_tok))

def upsert_embedding(entity_id: str, embedding: list[float]):
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
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (node_key,))

def delete_node_cascade(node_key: str):
    conn = get_connection()
    with transaction():
        rows = conn.execute(
            "SELECT entity_id, canonical_name, data_json FROM entities "
            "WHERE entity_id = ? OR canonical_name = ? "
            "OR json_extract(data_json, '$.page_key') = ?",
            (node_key, node_key, node_key),
        ).fetchall()
        entity_ids = {row["entity_id"] for row in rows}
        related_ids = sorted(entity_ids | {node_key})
        placeholders = ",".join("?" for _ in related_ids)
        old_claim_rows = conn.execute(
            "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE "
            "json_extract(data_json, '$.locator.page_key') = ? OR "
            "json_extract(data_json, '$.source_page') IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        ).fetchall()
        old_evidence_rows = conn.execute(
            "SELECT evidence_id, data_json, updated_at FROM evidence WHERE "
            "json_extract(data_json, '$.locator.page_key') = ?",
            (node_key,),
        ).fetchall()

        # Deleting a canonical page must also close every durable projection that
        # can otherwise make the removed content look current.  Keep the history,
        # but turn it into an explicit tombstone inside this same transaction.
        now = datetime.now(timezone.utc).isoformat()
        claim_ids = sorted({str(row["claim_id"]) for row in old_claim_rows})
        memory_params: list[str] = [node_key, node_key + ".md"]
        memory_where = "json_extract(data_json, '$.source_page') IN (?, ?)"
        if claim_ids:
            claim_placeholders = ",".join("?" for _ in claim_ids)
            memory_where += (
                " OR json_extract(data_json, '$.source_claim_id') "
                f"IN ({claim_placeholders})"
            )
            memory_params.extend(claim_ids)
        memory_rows = conn.execute(
            f"SELECT memory_id, data_json FROM operational_memory WHERE {memory_where}",
            memory_params,
        ).fetchall()
        for memory_row in memory_rows:
            try:
                memory = json.loads(memory_row["data_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                memory = {"memory_id": memory_row["memory_id"]}
            reasons = memory.get("validity_reasons") or []
            if not isinstance(reasons, list):
                reasons = [str(reasons)]
            if "source_claim_deleted" not in reasons:
                reasons.append("source_claim_deleted")
            memory.update(
                {
                    "status": "Archived",
                    "validity_state": "archived",
                    "validity_reasons": reasons,
                    "deleted_at": now,
                    "updated_at": now,
                }
            )
            conn.execute(
                "UPDATE operational_memory SET status = 'Archived', data_json = ?, updated_at = ? "
                "WHERE memory_id = ?",
                (json.dumps(memory, ensure_ascii=False), now, memory_row["memory_id"]),
            )

        from vector_lake.governance_store import _append_version_records

        deleted_claims = []
        for row in old_claim_rows:
            record = json.loads(row["data_json"] or "{}")
            record.setdefault("claim_id", row["claim_id"])
            record.update({"status": "Archived", "lifecycle_state": "deleted", "deleted_at": now})
            deleted_claims.append(record)
        deleted_evidence = []
        for row in old_evidence_rows:
            record = json.loads(row["data_json"] or "{}")
            record.setdefault("evidence_id", row["evidence_id"])
            record.update({"status": "Archived", "lifecycle_state": "deleted", "deleted_at": now})
            deleted_evidence.append(record)
        _append_version_records(
            "claim_versions",
            "claim_id",
            "claim_family_id",
            "claimfamily",
            "claim_version",
            deleted_claims,
        )
        _append_version_records(
            "evidence_versions",
            "evidence_id",
            "evidence_family_id",
            "evidencefamily",
            "evidence_version",
            deleted_evidence,
        )

        for entity_row in rows:
            try:
                identity = json.loads(entity_row["data_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                identity = {}
            identity.update(
                {
                    "entity_id": entity_row["entity_id"],
                    "page_key": node_key,
                    "canonical_name": entity_row["canonical_name"],
                    "lifecycle_state": "deleted",
                    "deleted_at": now,
                }
            )
            conn.execute(
                "INSERT INTO entity_identities "
                "(entity_id, page_key, canonical_name, identity_origin, data_json, recorded_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_id) DO UPDATE SET "
                "canonical_name = excluded.canonical_name, data_json = excluded.data_json, "
                "updated_at = excluded.updated_at",
                (
                    entity_row["entity_id"],
                    node_key,
                    entity_row["canonical_name"],
                    str(identity.get("identity_origin") or "deletion_backfill"),
                    json.dumps(identity, ensure_ascii=False),
                    now,
                    now,
                ),
            )

        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute(f"DELETE FROM vec_embeddings WHERE entity_id IN ({placeholders})", related_ids)
        conn.execute(
            "DELETE FROM claims WHERE "
            "json_extract(data_json, '$.locator.page_key') = ? OR "
            "json_extract(data_json, '$.source_page') IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        )
        conn.execute(
            "DELETE FROM evidence WHERE json_extract(data_json, '$.locator.page_key') = ?",
            (node_key,),
        )
        conn.execute(
            "DELETE FROM sources WHERE source_id = ? OR "
            "json_extract(data_json, '$.canonical_source_page') = ?",
            (node_key, node_key + ".md"),
        )
        conn.execute(f"DELETE FROM alias_registry WHERE key = ? OR value IN ({placeholders})", [node_key, *related_ids])
        conn.execute(f"DELETE FROM claim_graph_nodes WHERE node_id IN ({placeholders})", related_ids)
        conn.execute(
            f"DELETE FROM claim_graph_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            [*related_ids, *related_ids],
        )
        from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta

        sync_timeline_events_for_claim_delta(old_claim_rows, [])
        conn.execute(f"DELETE FROM entities WHERE entity_id IN ({placeholders})", related_ids)

    return {"page_key": node_key, "entity_ids": sorted(entity_ids)}


def enqueue_mutation(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
    idempotency_key: str | None = None,
    validation_mode: str = "full",
    base_version: str | None = None,
    projection_base_hash: str | None = None,
) -> int:
    if mutation_type not in {"update", "delete"}:
        raise ValueError(f"Unsupported mutation_type: {mutation_type}")
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    init_db()
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        current_id = None
        if idempotency_key:
            existing = conn.execute(
                "SELECT id, status FROM mutation_outbox WHERE idempotency_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if existing:
                newer = conn.execute(
                    "SELECT id FROM mutation_outbox WHERE filename = ? AND id > ? "
                    "AND status != 'superseded' ORDER BY id DESC LIMIT 1",
                    (filename, existing["id"]),
                ).fetchone()
                if newer is None:
                    if existing["status"] == "failed":
                        conn.execute(
                            "UPDATE mutation_outbox SET status = 'pending', attempt_count = 0, "
                            "last_error = NULL, available_at = ?, completed_at = NULL, "
                            "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
                            "superseded_by = NULL WHERE id = ?",
                            (now, existing["id"]),
                        )
                        current_id = int(existing["id"])
                    else:
                        return int(existing["id"])
        if current_id is None:
            cursor = conn.execute(
                "INSERT INTO mutation_outbox "
            "(filename, mutation_type, payload_text, status, attempt_count, created_at, available_at, "
            "idempotency_key, validation_mode, base_version, projection_base_hash) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)",
                (
                    filename,
                    mutation_type,
                    payload_text,
                    now,
                    now,
                    idempotency_key,
                    validation_mode,
                    base_version,
                    projection_base_hash,
                ),
            )
            current_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE mutation_outbox SET status = 'superseded', superseded_by = ?, "
            "completed_at = COALESCE(completed_at, ?), lease_until = NULL, "
            "lease_owner = NULL, lease_token = NULL "
            "WHERE filename = ? AND id != ? AND status IN ('pending', 'processing')",
            (current_id, now, filename, current_id),
        )
        return current_id


def is_managed_projection_state(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
) -> bool:
    """Return whether a filesystem event matches the latest durable projection intent."""
    init_db()
    row = get_connection().execute(
        "SELECT mutation_type, payload_text, status FROM mutation_outbox "
        "WHERE filename = ? AND status != 'superseded' ORDER BY id DESC LIMIT 1",
        (str(filename),),
    ).fetchone()
    if row is None:
        return False
    if str(row["mutation_type"]) != str(mutation_type):
        return False
    if mutation_type == "delete":
        return True
    if row["payload_text"] is None or payload_text is None:
        return row["payload_text"] == payload_text
    return normalize_semantic_text(row["payload_text"]) == normalize_semantic_text(
        payload_text
    )


def claim_mutation_outbox(
    limit: int = 50,
    lease_seconds: int = 120,
    lease_owner: str | None = None,
    outbox_ids: list[int] | None = None,
) -> list[dict]:
    """Atomically claim ready rows, including abandoned processing leases."""
    import os
    import secrets
    import socket

    init_db()
    conn = get_connection()
    owner = lease_owner or os.environ.get("VECTOR_LAKE_OUTBOX_RUN_ID") or f"{socket.gethostname()}:{os.getpid()}"
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
    with transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'superseded', "
            "superseded_by = ("
            "  SELECT MAX(newer.id) FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            "), completed_at = COALESCE(completed_at, ?), lease_until = NULL, "
            "lease_owner = NULL, lease_token = NULL "
            "WHERE status IN ('pending', 'processing') AND EXISTS ("
            "  SELECT 1 FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            ")",
            (now,),
        )
        requested_ids = sorted({int(value) for value in (outbox_ids or [])})
        query = (
            "SELECT id FROM mutation_outbox WHERE ((status = 'pending' "
            "AND COALESCE(available_at, created_at, '') <= ?) OR "
            "(status = 'processing' AND COALESCE(lease_until, '') <= ?))"
        )
        params: list = [now, now]
        if requested_ids:
            placeholders = ",".join("?" for _ in requested_ids)
            query += f" AND id IN ({placeholders})"
            params.extend(requested_ids)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(query, tuple(params)).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []
        claimed_ids = []
        for outbox_id in ids:
            token = secrets.token_hex(16)
            claimed = conn.execute(
                "UPDATE mutation_outbox SET status = 'processing', "
                "attempt_count = COALESCE(attempt_count, 0) + 1, started_at = ?, "
                "lease_until = ?, lease_owner = ?, lease_token = ?, "
                "lease_generation = COALESCE(lease_generation, 0) + 1 "
                "WHERE id = ? AND ((status = 'pending' AND COALESCE(available_at, created_at, '') <= ?) "
                "OR (status = 'processing' AND COALESCE(lease_until, '') <= ?))",
                (now, lease_until, owner, token, outbox_id, now, now),
            )
            if claimed.rowcount:
                claimed_ids.append(outbox_id)
        if not claimed_ids:
            return []
        placeholders = ",".join("?" for _ in claimed_ids)
        claimed = conn.execute(
            f"SELECT * FROM mutation_outbox WHERE id IN ({placeholders}) ORDER BY id ASC",
            claimed_ids,
        ).fetchall()
        return [dict(row) for row in claimed]


def mutation_outbox_lease_is_current(
    outbox_id: int,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    row = get_connection().execute(
        "SELECT 1 FROM mutation_outbox WHERE id = ? AND status = 'processing' "
        "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
        "AND COALESCE(lease_until, '') > ?",
        (outbox_id, lease_owner, lease_token, int(lease_generation), now),
    ).fetchone()
    return row is not None


def mutation_outbox_is_latest_intent(outbox_id: int) -> bool:
    row = get_connection().execute(
        "SELECT 1 FROM mutation_outbox AS current WHERE current.id = ? "
        "AND current.status != 'superseded' AND NOT EXISTS ("
        "  SELECT 1 FROM mutation_outbox AS newer "
        "  WHERE newer.filename = current.filename AND newer.id > current.id "
        "    AND newer.status != 'superseded'"
        ")",
        (int(outbox_id),),
    ).fetchone()
    return row is not None


def mutation_outbox_statuses(outbox_ids: list[int]) -> dict[int, str]:
    ids = sorted({int(value) for value in outbox_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = get_connection().execute(
        f"SELECT id, status FROM mutation_outbox WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return {int(row["id"]): str(row["status"]) for row in rows}


def record_merge_journal(
    journal_id: str,
    item_id: str,
    data: dict,
    status: str = "prepared",
) -> dict:
    """Persist an immutable pre-merge snapshot inside the caller transaction."""
    now = datetime.now(timezone.utc).isoformat()
    payload = dict(data)
    payload.setdefault("journal_id", str(journal_id))
    payload.setdefault("item_id", str(item_id))
    payload.setdefault("created_at", now)
    with transaction():
        get_connection().execute(
            "INSERT OR IGNORE INTO merge_journal "
            "(journal_id, item_id, status, data_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(journal_id),
                str(item_id),
                str(status),
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
    return get_merge_journal(journal_id) or payload


def update_merge_journal(journal_id: str, updates: dict, status: str | None = None) -> dict | None:
    """Update merge execution metadata without replacing the pre-merge snapshot."""
    with transaction():
        row = get_connection().execute(
            "SELECT status, data_json FROM merge_journal WHERE journal_id = ?",
            (str(journal_id),),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["data_json"] or "{}")
        payload.update(dict(updates))
        next_status = str(status or row["status"])
        now = datetime.now(timezone.utc).isoformat()
        get_connection().execute(
            "UPDATE merge_journal SET status = ?, data_json = ?, updated_at = ? "
            "WHERE journal_id = ?",
            (next_status, json.dumps(payload, ensure_ascii=False), now, str(journal_id)),
        )
    return {**payload, "status": next_status}


def get_merge_journal(journal_id: str) -> dict | None:
    row = get_connection().execute(
        "SELECT status, data_json, created_at, updated_at FROM merge_journal WHERE journal_id = ?",
        (str(journal_id),),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["data_json"] or "{}")
    payload.update(
        {
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
    )
    return payload


def complete_mutation_outbox(
    outbox_id: int,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
) -> bool:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        updated = conn.execute(
            "UPDATE mutation_outbox SET status = 'completed', completed_at = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL, last_error = NULL "
            "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
            "AND lease_token = ? AND lease_generation = ? AND COALESCE(lease_until, '') > ?",
            (now, outbox_id, lease_owner, lease_token, int(lease_generation), now),
        )
    return bool(updated.rowcount)


def fail_mutation_outbox(
    outbox_id: int,
    error: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
) -> str:
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with transaction():
        row = conn.execute(
            "SELECT attempt_count FROM mutation_outbox WHERE id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (outbox_id, lease_owner, lease_token, int(lease_generation), now),
        ).fetchone()
        if row is None:
            return "stale"
        attempts = int(row["attempt_count"] or 0)
        terminal = attempts >= max(1, int(max_attempts))
        status = "failed" if terminal else "pending"
        delay_seconds = 0.0 if terminal else max(0.0, float(backoff_base)) * (2 ** max(0, attempts - 1))
        available_at = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        updated = conn.execute(
            "UPDATE mutation_outbox SET status = ?, last_error = ?, available_at = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL WHERE id = ? "
            "AND status = 'processing' AND lease_owner = ? AND lease_token = ? "
            "AND lease_generation = ? AND COALESCE(lease_until, '') > ?",
            (
                status,
                str(error)[:4000],
                available_at,
                outbox_id,
                lease_owner,
                lease_token,
                int(lease_generation),
                now,
            ),
        )
        return status if updated.rowcount else "stale"

def search_wiki(query: str, limit: int = 50) -> list[dict]:
    import re
    query = re.sub(r'[^\w\s\u4e00-\u9fa5]', ' ', query) if query else ""
    try:
        from vector_lake.tokenizer_runtime import tokenize_for_fts
        query_tok = tokenize_for_fts(query)
    except ImportError:
        query_tok = query if query else ""

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

def mark_file_processed(filepath: str, file_hash: str):
    from datetime import datetime, timezone
    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute("""
            INSERT INTO processed_files (filepath, file_hash, processed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                file_hash = excluded.file_hash,
                processed_at = excluded.processed_at
        """, (filepath, file_hash, now_str))

def enqueue_job(task_type: str, payload: dict, idempotency_key: str | None = None) -> str:
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
                return str(existing["job_id"])
        conn.execute("""
            INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at, available_at, idempotency_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, task_type, json.dumps(payload, ensure_ascii=False), "queued", now_str, now_str, now_str, key))
    return job_id


def claim_pending_jobs(limit: int = 10, lease_seconds: int = 300) -> list[dict]:
    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
    with transaction():
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE "
            "((status IN ('queued', 'failed') AND retries < 3 AND COALESCE(available_at, created_at, '') <= ?) "
            "OR (status = 'dispatched' AND COALESCE(lease_until, '') <= ?)) "
            "ORDER BY created_at ASC LIMIT ?",
            (now, now, max(1, int(limit))),
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
    expected_contract_version = payload.get("ingest_contract_version")
    if expected_contract_version is not None and (
        str(processed_data.get("ingest_contract_version") or "")
        != str(expected_contract_version)
    ):
        raise ValueError(f"Job {job_id} ingest_contract_version does not match its queued payload")
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
