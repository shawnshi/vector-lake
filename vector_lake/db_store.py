import sqlite3
import threading
from pathlib import Path
from vector_lake.wiki_utils import get_meta_dir

_LOCAL = threading.local()

def get_db_path() -> Path:
    return get_meta_dir() / "vector_lake.db"

def get_connection() -> sqlite3.Connection:
    if not hasattr(_LOCAL, "conn") or _LOCAL.conn is None:
        db_path = get_db_path()
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        # Enable WAL mode for high concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Enable foreign keys
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _LOCAL.conn = conn
    return _LOCAL.conn

def close_connection():
    if hasattr(_LOCAL, "conn") and _LOCAL.conn is not None:
        _LOCAL.conn.close()
        _LOCAL.conn = None

def init_db():
    conn = get_connection()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                canonical_name TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
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
                tokenize='porter unicode61 remove_diacritics 1'
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



def upsert_search_index(node_key: str, title: str, summary: str, text: str):
    conn = get_connection()
    with conn:
        # FTS5 doesn't support ON CONFLICT REPLACE directly, so we delete then insert.
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("""
            INSERT INTO wiki_search_index (node_key, title, summary, text)
            VALUES (?, ?, ?, ?)
        """, (node_key, title, summary, text))

def delete_search_index(node_key: str):
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))

def search_wiki(query: str, limit: int = 50) -> list[dict]:
    conn = get_connection()
    cur = conn.execute("""
        SELECT node_key, title, summary, bm25(wiki_search_index) as rank 
        FROM wiki_search_index 
        WHERE wiki_search_index MATCH ? 
        ORDER BY rank LIMIT ?
    """, (query, limit))
    return [dict(row) for row in cur.fetchall()]
