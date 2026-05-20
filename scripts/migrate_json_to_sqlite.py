import sys
import os
import json
import sqlite3
import datetime
from pathlib import Path

sys.path.insert(0, r"C:\Users\shich\.gemini\extensions\vector-lake")
from vector_lake.wiki_utils import get_meta_dir, get_claim_graph_path
from vector_lake.db_store import init_db, get_connection, close_connection

def load_json(path: Path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def migrate():
    meta_dir = get_meta_dir()
    print(f"Initializing DB at {meta_dir / 'vector_lake.db'}")
    init_db()
    conn = get_connection()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # 1. Migrate Maps
    maps = {
        "entities.json": ("entities", "entity_id", "canonical_name"),
        "claims.json": ("claims", "claim_id", "claim_text", "status"),
        "evidence.json": ("evidence", "evidence_id"),
        "sources.json": ("sources", "source_id"),
        "operational_memory.json": ("operational_memory", "memory_id", "memory_type", "score")
    }
    
    for filename, cols in maps.items():
        data = load_json(meta_dir / filename)
        items = data.get("items", {})
        print(f"Migrating {len(items)} items from {filename}")
        
        table = cols[0]
        pk = cols[1]
        
        with conn:
            for k, v in items.items():
                extra_args = []
                for extra_col in cols[2:]:
                    if extra_col == "score":
                        extra_args.append(float(v.get("memory_score") or 0.0))
                    else:
                        extra_args.append(str(v.get(extra_col, "")))
                
                col_names = [pk] + list(cols[2:]) + ["data_json", "updated_at"]
                placeholders = ["?"] * len(col_names)
                query = f"INSERT OR REPLACE INTO {table} ({', '.join(col_names)}) VALUES ({', '.join(placeholders)})"
                params = [k] + extra_args + [json.dumps(v, ensure_ascii=False), now]
                conn.execute(query, params)
                
    # 2. Migrate Queues
    queues = {
        "change_sets.json": ("change_sets", "change_set_id"),
        "governance_queue.json": ("governance_queue", "item_id")
    }
    
    for filename, cols in queues.items():
        data = load_json(meta_dir / filename)
        items = data.get("items", [])
        print(f"Migrating {len(items)} items from {filename}")
        table = cols[0]
        pk = cols[1]
        
        with conn:
            for item in items:
                k = item.get(pk)
                if not k:
                    import uuid
                    k = uuid.uuid4().hex
                conn.execute(f"INSERT OR REPLACE INTO {table} ({pk}, data_json, updated_at) VALUES (?, ?, ?)", (k, json.dumps(item, ensure_ascii=False), now))

    # 3. Migrate Alias Registry
    aliases = load_json(meta_dir / "alias_registry.json").get("items", {})
    print(f"Migrating {len(aliases)} items from alias_registry.json")
    with conn:
        for k, v in aliases.items():
            conn.execute("INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)", (k, v, now))

    # 4. Migrate Claim Graph
    claim_graph = load_json(get_claim_graph_path())
    nodes = claim_graph.get("nodes", [])
    edges = claim_graph.get("edges", [])
    print(f"Migrating {len(nodes)} nodes and {len(edges)} edges from claim_graph.json")
    
    with conn:
        for node in nodes:
            k = node.get("id")
            if k:
                conn.execute("INSERT OR REPLACE INTO claim_graph_nodes (node_id, data_json, updated_at) VALUES (?, ?, ?)", (k, json.dumps(node, ensure_ascii=False), now))
                
        for edge in edges:
            src = edge.get("source")
            tgt = edge.get("target")
            rel = edge.get("relation", "links_to")
            weight = edge.get("weight", 1.0)
            conn.execute("INSERT OR REPLACE INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) VALUES (?, ?, ?, ?, ?)", (src, tgt, rel, weight, now))

    print("Migration complete!")
    close_connection()

if __name__ == "__main__":
    migrate()
