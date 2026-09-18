import uuid
from datetime import datetime, timezone
from vector_lake.wiki_utils import get_wiki_dir
from vector_lake import governance_store


def _entity_id_for_page_key(page_key: str) -> str | None:
    """The registered entity id behind a wiki page, or ``None`` when unregistered.

    The candidate used to carry ``f"entity_{page_key}"`` here, which is not an id any
    row has: ``governance_store.upsert_alias`` then recorded a mapping between two
    invented strings, so the merge's own alias bookkeeping pointed at nothing and the
    cycle guard downstream could not see the real chain.  Resolving the real id is the
    only way ``resolve_governance_item`` can write a usable ``alias_registry`` row.
    """
    entities = governance_store.query_entities({"f_page_key": page_key})["items"]
    return next(iter(entities), None)


def bulk_reconcile(operations: list, dry_run: bool = True) -> str:
    if not isinstance(operations, list):
        return f"[Sandbox JSON Error] Expected list of operations, got {type(operations)}"
    
    if not operations:
        return "No operations to perform."

    from pathlib import Path
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    
    # Pre-flight
    replace_map = {}
    entity_ids = {}
    for op in operations:
        src = op.get("source_entity")
        tgt = op.get("target_entity")
        if not src or not tgt:
            return "Error: Each operation must have source_entity and target_entity."
        if src.endswith('.md'): src = src[:-3]
        if tgt.endswith('.md'): tgt = tgt[:-3]
        
        src_path = (wiki_dir / f"{src}.md").resolve()
        tgt_path = (wiki_dir / f"{tgt}.md").resolve()
        if not src_path.is_relative_to(wiki_dir) or not tgt_path.is_relative_to(wiki_dir):
            return f"[Security Error] Source '{src}' or target '{tgt}' resolves outside wiki directory."
        
        replace_map[src] = tgt
        for page_key in (src, tgt):
            if page_key not in entity_ids:
                entity_ids[page_key] = _entity_id_for_page_key(page_key)

    unregistered = sorted(
        page_key for page_key, entity_id in entity_ids.items() if not entity_id
    )
    if unregistered:
        return (
            "Error: No entity row for page_key(s) "
            + ", ".join(unregistered)
            + ". Refusing to enqueue a merge candidate whose entity ids would have to be "
            "invented; index the page(s) first, then re-run."
        )

    for k in list(replace_map.keys()):
        curr = replace_map[k]
        visited = {k}
        while curr in replace_map:
            if curr in visited:
                return f"Error: Circular reference detected involving {curr}."
            visited.add(curr)
            curr = replace_map[curr]
        replace_map[k] = curr

    if dry_run:
        return (
            f"[DRY RUN] Validated {len(operations)} operations against "
            f"{len(entity_ids)} registered entity row(s). No cycles detected. "
            f"Would enqueue {len(operations)} merge tasks to the governance queue."
        )

    # Enqueue to governance queue under the shared queue lock so a concurrent
    # writer's items are never dropped by this snapshot-and-save cycle.
    enqueued = 0
    now_str = datetime.now(timezone.utc).isoformat()
    with governance_store.governance_queue_session():
        queue = governance_store.load_governance_queue()
        for src, tgt in replace_map.items():
            # Prevent duplicating items
            if any(
                item.get("merge_source") == src and item.get("merge_target") == tgt
                for item in queue.get("items", [])
            ):
                continue

            item = {
                "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                "type": "merge",
                "title": f"Merge {src} into {tgt}",
                "description": f"Bulk reconcile tool requested merge of {src} into {tgt}.",
                "created_at": now_str,
                "status": "pending",
                "source": "bulk_reconcile",
                "affected_pages": [f"{src}.md", f"{tgt}.md"],
                "merge_candidate": {
                    "left_name": tgt,
                    "right_name": src,
                    "left_entity_id": entity_ids[tgt],
                    "right_entity_id": entity_ids[src],
                },
            }
            queue.setdefault("items", []).append(item)
            enqueued += 1

        if enqueued > 0:
            governance_store.save_governance_queue(queue)

    return f"Success: Enqueued {enqueued} merge suggestions to the governance queue. Awaiting Mentat review."
