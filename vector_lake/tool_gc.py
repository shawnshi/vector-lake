import json
import logging
import os
import time

from vector_lake.wiki_utils import get_index_path, get_wiki_dir

log = logging.getLogger("vector-lake-gc")


def gc_vector_lake(days: int = 30, dry_run: bool = False) -> str:
    index_path = get_index_path()
    if not index_path.exists():
        return "No index found. Please run 'python cli.py sync' first."

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    except json.JSONDecodeError:
        return "Failed to parse index.json."

    nodes = index_data.get("nodes", {})
    edges = index_data.get("weighted_edges", [])

    degrees = {key: 0 for key in nodes.keys()}
    for edge in edges:
        if edge["source"] in degrees:
            degrees[edge["source"]] += 1
        if edge["target"] in degrees:
            degrees[edge["target"]] += 1

    wiki_dir = get_wiki_dir()
    now = time.time()
    cutoff = now - (days * 86400)

    orphans = []
    for key, node in nodes.items():
        if node.get("type") != "entity":
            continue
        if degrees[key] <= 1:
            file_path = wiki_dir / f"{key}.md"
            if file_path.exists():
                mtime = os.path.getmtime(file_path)
                if mtime < cutoff:
                    orphans.append(file_path)

    if dry_run:
        if not orphans:
            return f"[DRY-RUN] No orphan entities older than {days} days found."
        lines = [f"[DRY-RUN] Found {len(orphans)} orphan entities older than {days} days (Degree <= 1):"]
        for p in orphans[:20]:
            lines.append(f"  - {p.name}")
        if len(orphans) > 20:
            lines.append(f"  ... and {len(orphans) - 20} more.")
        return "\n".join(lines)

    if not orphans:
        return f"GC complete. No orphan entities older than {days} days found."

    deleted = 0
    for path in orphans:
        try:
            os.remove(path)
            deleted += 1
        except OSError as e:
            log.warning(f"Failed to delete {path.name}: {e}")

    return f"GC complete. Deleted {deleted} orphan entities older than {days} days."
