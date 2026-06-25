import os
from vector_lake.db_store import get_connection

def get_processed_files() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT filepath, file_hash, processed_at FROM processed_files").fetchall()
        return {row["filepath"]: {"hash": row["file_hash"], "processed_at": row["processed_at"]} for row in rows}
    except Exception:
        return {}

def mark_file_processed(filepath: str, file_hash: str, timestamp: str):
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO processed_files (filepath, file_hash, processed_at) VALUES (?, ?, ?)",
                (filepath, file_hash, timestamp)
            )
    except Exception:
        pass
