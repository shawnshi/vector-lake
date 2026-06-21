import sqlite3
import json
import os
import sys

# Ensure vector_lake is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from vector_lake.db_store import get_connection

def migrate():
    print("Starting Vector Lake DB Migration to V10...")
    conn = get_connection()
    
    with conn:
        # 1. Add new columns
        tables_to_alter = {
            "entities": [
                ("type", "TEXT"),
                ("status", "TEXT"),
                ("ttl", "INTEGER"),
                ("decay_weight", "REAL")
            ],
            "operational_memory": [
                ("status", "TEXT"),
                ("ttl", "INTEGER")
            ]
        }
        
        for table, columns in tables_to_alter.items():
            for col_name, col_type in columns:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    print(f"Added column {col_name} to {table}")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" in str(e):
                        pass
                    else:
                        print(f"Warning: {e}")

        # 2. Populate data
        print("Populating new columns in entities...")
        rows = conn.execute("SELECT entity_id, data_json FROM entities").fetchall()
        for row in rows:
            data = json.loads(row["data_json"])
            type_val = data.get("type", "concept")
            status_val = data.get("status", "Active")
            ttl_val = data.get("ttl", 1825)
            decay_val = data.get("decay_weight", 1.0)
            
            conn.execute(
                "UPDATE entities SET type = ?, status = ?, ttl = ?, decay_weight = ? WHERE entity_id = ?",
                (type_val, status_val, ttl_val, decay_val, row["entity_id"])
            )
            
        print("Populating new columns in operational_memory...")
        rows = conn.execute("SELECT memory_id, data_json FROM operational_memory").fetchall()
        for row in rows:
            data = json.loads(row["data_json"])
            status_val = data.get("status", "active")
            ttl_val = data.get("ttl", 365)
            
            conn.execute(
                "UPDATE operational_memory SET status = ?, ttl = ? WHERE memory_id = ?",
                (status_val, ttl_val, row["memory_id"])
            )
            
        # 3. Create Indexes
        print("Creating indexes...")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type_status ON entities(type, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type_status ON operational_memory(memory_type, status)")
        
    print("Migration V10 completed successfully.")

if __name__ == "__main__":
    migrate()
