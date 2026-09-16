import json
import logging
import os
import time

from vector_lake.wiki_utils import get_wiki_dir

log = logging.getLogger("vector-lake-gc")


def gc_vector_lake(days: int = 30, dry_run: bool = True, force: bool = False) -> str:
    from vector_lake.governance_store import load_claims, load_entities

    entities = load_entities().get("items", {})
    nodes = {
        str(node.get("page_key") or entity_id): node
        for entity_id, node in entities.items()
    }
    identity = {
        str(entity_id): str(node.get("page_key") or entity_id)
        for entity_id, node in entities.items()
    }
    canonical_id_of = {page_key: entity_id for entity_id, page_key in identity.items()}

    # Orphan detection needs topological connectivity, not the weighted/pruned
    # edge set used to render the force graph.  The visualization edges have a
    # relevance threshold that drops weak-but-real relations, which made every
    # page look isolated.
    degrees = _topological_degrees(nodes, identity, load_claims().get("items", {}))

    wiki_dir = get_wiki_dir()
    now = time.time()
    cutoff = now - (days * 86400)

    orphans = []
    typed_pages = 0
    for page_key, node in nodes.items():
        if str(node.get("type") or "").lower() not in ("vendor", "product", "person", "event"):
            continue
        typed_pages += 1
        if degrees.get(page_key, 0) <= 1:
            file_path = wiki_dir / f"{page_key}.md"
            if file_path.exists():
                mtime = os.path.getmtime(file_path)
                if mtime < cutoff:
                    orphans.append((file_path, canonical_id_of.get(page_key, page_key), degrees.get(page_key, 0)))

    if dry_run:
        if not orphans:
            return f"[DRY-RUN] No orphan entities older than {days} days found."
        lines = [
            f"[DRY-RUN] Found {len(orphans)} orphan entities older than {days} days "
            f"(page-edge degree <= 1) out of {typed_pages} typed pages. "
            f"Re-run with dry_run=False to execute:"
        ]
        for p, nid, degree in orphans[:20]:
            lines.append(f"  - {p.name} (ID: {nid}, degree: {degree})")
        if len(orphans) > 20:
            lines.append(f"  ... and {len(orphans) - 20} more.")
        return "\n".join(lines)

    if not orphans:
        return f"GC complete. No orphan entities older than {days} days found."

    # Mass-deletion circuit breaker: a degree signal that collapses for any
    # structural reason must not silently remove most of the entity corpus.
    if not force and len(orphans) >= 5 and len(orphans) > typed_pages * 0.5:
        return (
            f"GC aborted: {len(orphans)} of {typed_pages} typed pages ({len(orphans) / max(1, typed_pages):.0%}) "
            "would be deleted, which exceeds the 50% safety threshold. "
            "Inspect the page-key edge projection first (`projection-report`), then re-run with force=True "
            "if the corpus really is that sparse."
        )

    deleted = 0
    skipped = []
    import shutil
    backup_dir = wiki_dir.parent / "backup" / "gc" / _utc_stamp()
    backup_dir.mkdir(parents=True, exist_ok=True)

    for path, nid, degree in orphans:
        try:
            shutil.copy2(path, backup_dir / path.name)
            from vector_lake.mutation_coordinator import execute_mutation_plan
            execute_mutation_plan(path.name, is_delete=True)
            deleted += 1
        except Exception as e:
            skipped.append(path.name)
            log.error(f"Failed to GC {path.name}: {e}")

    prune_note = _prune_change_sets(cutoff, backup_dir)

    result = f"GC complete. Deleted {deleted} orphan pages (backed up to {backup_dir})."
    if skipped:
        result += f" Skipped: {', '.join(skipped[:10])}."
    return result + prune_note


def _topological_degrees(nodes: dict, identity: dict, claims: dict) -> dict:
    """Undirected neighbour count per page, from canonical relations.

    Sources of connectivity, all biased toward *not* declaring an orphan:
      * explicit ``links`` / ``outbound_links`` between pages,
      * pages sharing at least one raw source,
      * pages whose entities co-occur in the same claim.
    """
    keys = set(nodes)
    resolve = {}
    for key, node in nodes.items():
        resolve[key] = key
        for alias in [node.get("title"), *(node.get("aliases") or [])]:
            if alias:
                resolve.setdefault(str(alias), key)

    adjacency = {key: set() for key in keys}

    def _connect(left: str, right: str):
        if left != right and left in keys and right in keys:
            adjacency[left].add(right)
            adjacency[right].add(left)

    source_to_pages = {}
    for key, node in nodes.items():
        for link in [*(node.get("links") or []), *(node.get("outbound_links") or [])]:
            _connect(key, resolve.get(str(link), str(link)))
        for source in node.get("sources") or []:
            source_to_pages.setdefault(str(source), []).append(key)
    for pages in source_to_pages.values():
        for index, left in enumerate(pages):
            for right in pages[index + 1:]:
                _connect(left, right)

    claim_to_pages = {}
    for claim in claims.values():
        pages = {identity[entity_id] for entity_id in claim.get("subject_entity_ids") or [] if entity_id in identity}
        for page in pages:
            claim_to_pages.setdefault(page, set()).update(pages - {page})
    for page, peers in claim_to_pages.items():
        for peer in peers:
            _connect(page, peer)

    return {key: len(neighbours) for key, neighbours in adjacency.items()}


def _utc_stamp() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def _prune_change_sets(cutoff: float, backup_dir) -> str:
    """Prune the change-set ledger after rotating a copy of the affected rows."""
    import datetime

    from vector_lake.db_store import get_connection, transaction

    cutoff_dt = datetime.datetime.fromtimestamp(cutoff, datetime.timezone.utc).isoformat()
    conn = get_connection()
    with transaction():
        rows = conn.execute(
            "SELECT change_set_id, data_json FROM change_sets WHERE updated_at < ?", (cutoff_dt,)
        ).fetchall()
        if not rows:
            return ""
        ledger_backup = backup_dir / f"change_sets_before_{cutoff_dt[:10]}.jsonl"
        try:
            with open(ledger_backup, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            {"change_set_id": row["change_set_id"], "data_json": row["data_json"]},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except OSError as exc:
            log.error(f"Change-set prune aborted; could not rotate ledger backup: {exc}")
            return f" Change-set prune skipped (backup failed: {exc})."
        conn.execute("DELETE FROM change_sets WHERE updated_at < ?", (cutoff_dt,))
    log.info(f"Database GC: pruned {len(rows)} change_sets older than {cutoff_dt}; backup={ledger_backup}")
    return f" Pruned {len(rows)} change_sets older than {cutoff_dt[:10]} (ledger backup: {ledger_backup.name})."
