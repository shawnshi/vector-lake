import sqlite3
import threading
from pathlib import Path
from vector_lake.wiki_utils import get_meta_dir
import sqlite_vec

_LOCAL = threading.local()

def get_db_path() -> Path:
    return get_meta_dir() / "vector_lake.db"

def get_connection() -> sqlite3.Connection:
    if getattr(_LOCAL, "conn", None) is None:
        db_path = get_db_path()
        conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        
        # Load sqlite-vec extension
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        
        _LOCAL.conn = conn
    return _LOCAL.conn

def close_connection():
    if hasattr(_LOCAL, "conn") and _LOCAL.conn is not None:
        _LOCAL.conn.close()
        _LOCAL.conn = None

from contextlib import contextmanager

@contextmanager
def transaction():
    conn = get_connection()
    in_tx = getattr(_LOCAL, 'in_transaction', False)
    if in_tx:
        yield conn
    else:
        _LOCAL.in_transaction = True
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _LOCAL.in_transaction = False

def init_db():
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
            CREATE TABLE IF NOT EXISTS change_sets (
                change_set_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
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
        
        # Add expression-based indexes for performance
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (json_extract(data_json, '$.type'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_status ON entities (json_extract(data_json, '$.status'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON operational_memory (json_extract(data_json, '$.memory_type'))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON operational_memory (json_extract(data_json, '$.status'))")
        except sqlite3.OperationalError as e:
            # Older SQLite versions might not support expression indexes
            import logging
            logging.getLogger("vector-lake-db").warning(f"Could not create JSON expression indexes: {e}")



def upsert_search_index(node_key: str, title: str, summary: str, text: str):
    try:
        import jieba
        title_tok = " ".join(jieba.cut(title)) if title else ""
        summary_tok = " ".join(jieba.cut(summary)) if summary else ""
        text_tok = " ".join(jieba.cut(text)) if text else ""
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

def delete_search_index(node_key: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (node_key,))

def delete_node_cascade(node_key: str):
    conn = get_connection()
    with transaction():
        cur = conn.execute("SELECT entity_id FROM entities WHERE canonical_name = ?", (node_key,))
        row = cur.fetchone()
        ent_id = row[0] if row else node_key
        
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM entities WHERE entity_id = ? OR canonical_name = ?", (ent_id, node_key))
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (ent_id,))
        conn.execute("DELETE FROM claims WHERE json_extract(data_json, '$.source_page') IN (?, ?)", (node_key, node_key + ".md"))
        conn.execute("DELETE FROM claim_graph_nodes WHERE node_id IN (?, ?)", (node_key, ent_id))
        conn.execute("DELETE FROM claim_graph_edges WHERE source_id IN (?, ?) OR target_id IN (?, ?)", (node_key, ent_id, node_key, ent_id))
        conn.execute("DELETE FROM sources WHERE source_id = ?", (node_key,))
        conn.execute("DELETE FROM timeline_events WHERE entity_id = ?", (ent_id,))

def search_wiki(query: str, limit: int = 50) -> list[dict]:
    import re
    query = re.sub(r'[^\w\s\u4e00-\u9fa5]', ' ', query) if query else ""
    try:
        import jieba
        query_tok = " ".join(jieba.cut(query)) if query else ""
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

def enqueue_job(task_type: str, payload: dict) -> str:
    import uuid
    import json
    from datetime import datetime, timezone
    conn = get_connection()
    job_id = uuid.uuid4().hex
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute("""
            INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, task_type, json.dumps(payload, ensure_ascii=False), "queued", now_str, now_str))
    return job_id

def get_pending_jobs(limit: int = 10) -> list[dict]:
    import json
    conn = get_connection()
    cur = conn.execute("""
        SELECT * FROM jobs 
        WHERE status IN ('queued', 'dispatched') OR (status = 'failed' AND retries < 3)
        ORDER BY created_at ASC LIMIT ?
    """, (limit,))
    return [dict(row) for row in cur.fetchall()]

def update_job_status(job_id: str, status: str, error_msg: str = ""):
    from datetime import datetime, timezone
    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        if status == "failed":
            conn.execute("""
                UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?, retries = retries + 1
                WHERE job_id = ?
            """, (status, error_msg, now_str, job_id))
        else:
            conn.execute("""
                UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?
                WHERE job_id = ?
            """, (status, error_msg, now_str, job_id))



def backup_database():
    """Defend against single SQLite file SPOF."""
    import shutil, time
    from vector_lake.wiki_utils import get_extension_root
    db_path = get_extension_root() / "vector_lake.db"
    backup_dir = get_extension_root() / "backup"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"vector_lake_{int(time.time())}.db.bak"
    if db_path.exists():
        shutil.copy2(db_path, backup_path)
    return str(backup_path)
