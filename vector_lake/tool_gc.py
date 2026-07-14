import json
import logging
import os
import time

from vector_lake.wiki_utils import get_index_path, get_wiki_dir

log = logging.getLogger("vector-lake-gc")


def gc_vector_lake(days: int = 30, dry_run: bool = True) -> str:
    from vector_lake.governance_store import load_entities
    from vector_lake.db_store import get_connection
    
    entities = load_entities()
    nodes = {val.get("canonical_name", key): val for key, val in entities.get("items", {}).items()}
    
    conn = get_connection()
    edges = conn.execute("SELECT source_id, target_id FROM page_graph_edges").fetchall()
    
    degrees = {key: 0 for key in nodes.keys()}
    for row in edges:
        s, t = row[0], row[1]
        if s in degrees: degrees[s] += 1
        if t in degrees: degrees[t] += 1

    wiki_dir = get_wiki_dir()
    now = time.time()
    cutoff = now - (days * 86400)

    orphans = []
    for key, node in nodes.items():
        if node.get("type") not in ("vendor", "product", "person", "event"):
            continue
        if degrees[key] <= 1:
            file_path = wiki_dir / f"{key}.md"
            if file_path.exists():
                mtime = os.path.getmtime(file_path)
                if mtime < cutoff:
                    orphans.append((file_path, node.get("id")))

    if dry_run:
        if not orphans:
            return f"[DRY-RUN] No orphan entities older than {days} days found."
        lines = [f"[DRY-RUN] Found {len(orphans)} orphan entities older than {days} days (Degree <= 1). Re-run with dry_run=False to execute:"]
        for p, nid in orphans[:20]:
            lines.append(f"  - {p.name} (ID: {nid})")
        if len(orphans) > 20:
            lines.append(f"  ... and {len(orphans) - 20} more.")
        return "\n".join(lines)

    if not orphans:
        return f"GC complete. No orphan entities older than {days} days found."

    deleted = 0
    import shutil
    backup_dir = wiki_dir.parent / "backup" / "gc"
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    for path, nid in orphans:
        try:
            shutil.copy2(path, backup_dir / path.name)
            from vector_lake.mutation_coordinator import execute_mutation_plan
            execute_mutation_plan(path.name, is_delete=True)
            deleted += 1
        except Exception as e:
            log.error(f"Failed to GC {path.name}: {e}")

    try:
        from vector_lake.db_store import transaction
        import datetime
        cutoff_dt = datetime.datetime.utcfromtimestamp(cutoff).isoformat() + "Z"
        with transaction():
            conn.execute("DELETE FROM change_sets WHERE updated_at < ?", (cutoff_dt,))
        log.info(f"Database GC: Pruned change_sets older than {cutoff_dt}")
    except Exception as e:
        log.error(f"Failed to GC change_sets table: {e}")

    return f"GC complete. Deleted {deleted} orphan pages (backed up to {backup_dir})."
