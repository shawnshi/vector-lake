import logging
import os
import time
from datetime import datetime, timezone

from vector_lake.wiki_utils import get_wiki_dir

log = logging.getLogger("vector-lake-gc")


def prune_runtime_history(days: int = 30, dry_run: bool = True, now: float | None = None) -> dict:
    """Prune expired change-set history and its idempotency reservations atomically."""
    from vector_lake.db_store import get_connection, init_db, transaction

    normalized_days = max(1, int(days))
    cutoff_epoch = (time.time() if now is None else float(now)) - (normalized_days * 86400)
    cutoff = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()
    init_db()
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM change_sets WHERE updated_at < ?", (cutoff,)
    ).fetchone()
    candidate_count = int(row["count"] or 0)
    result = {
        "dry_run": bool(dry_run),
        "days": normalized_days,
        "cutoff": cutoff,
        "candidate_count": candidate_count,
        "pruned_change_sets": 0,
        "pruned_idempotency_keys": 0,
    }
    if dry_run or not candidate_count:
        return result
    with transaction():
        idempotency_result = conn.execute(
            "DELETE FROM change_set_idempotency WHERE change_set_id IN "
            "(SELECT change_set_id FROM change_sets WHERE updated_at < ?)",
            (cutoff,),
        )
        change_set_result = conn.execute(
            "DELETE FROM change_sets WHERE updated_at < ?", (cutoff,)
        )
    result["pruned_change_sets"] = int(change_set_result.rowcount)
    result["pruned_idempotency_keys"] = int(idempotency_result.rowcount)
    return result


def gc_vector_lake(days: int = 30, dry_run: bool = True) -> str:
    from vector_lake.governance_store import load_entities
    from vector_lake.db_store import get_connection
    
    entities = load_entities()
    nodes = {
        str(node.get("page_key") or entity_id): (str(entity_id), node)
        for entity_id, node in entities.get("items", {}).items()
    }
    
    conn = get_connection()
    edges = conn.execute(
        "SELECT source_id, target_id FROM claim_graph_edges "
        "UNION SELECT source_id, target_id FROM page_graph_edges"
    ).fetchall()
    
    degrees = {key: 0 for key in nodes.keys()}
    for row in edges:
        s, t = row[0], row[1]
        if s in degrees:
            degrees[s] += 1
        if t in degrees:
            degrees[t] += 1

    wiki_dir = get_wiki_dir()
    now = time.time()
    cutoff = now - (days * 86400)

    orphans = []
    for page_key, (entity_id, node) in nodes.items():
        if str(node.get("type") or "").lower() not in ("vendor", "product", "person", "event"):
            continue
        if degrees[page_key] <= 1:
            file_path = wiki_dir / f"{page_key}.md"
            if file_path.exists():
                mtime = os.path.getmtime(file_path)
                if mtime < cutoff:
                    orphans.append((file_path, entity_id))

    retention = prune_runtime_history(days=days, dry_run=dry_run, now=now)

    if dry_run:
        if not orphans:
            return (
                f"[DRY-RUN] No orphan entities older than {days} days found. "
                f"Retention would prune {retention['candidate_count']} change set(s)."
            )
        lines = [f"[DRY-RUN] Found {len(orphans)} orphan entities older than {days} days (Degree <= 1). Re-run with dry_run=False to execute:"]
        for p, nid in orphans[:20]:
            lines.append(f"  - {p.name} (ID: {nid})")
        if len(orphans) > 20:
            lines.append(f"  ... and {len(orphans) - 20} more.")
        lines.append(f"  Retention would prune {retention['candidate_count']} change set(s).")
        return "\n".join(lines)

    if not orphans:
        return (
            f"GC complete. No orphan entities older than {days} days found. "
            f"Pruned {retention['pruned_change_sets']} change set(s) and "
            f"{retention['pruned_idempotency_keys']} idempotency key(s)."
        )

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

    return (
        f"GC complete. Deleted {deleted} orphan pages (backed up to {backup_dir}). "
        f"Pruned {retention['pruned_change_sets']} change set(s) and "
        f"{retention['pruned_idempotency_keys']} idempotency key(s)."
    )
