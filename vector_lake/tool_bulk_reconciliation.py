import uuid
from datetime import datetime, timezone
from vector_lake.wiki_utils import get_wiki_dir
from vector_lake import governance_store

def bulk_reconcile(operations: list, dry_run: bool = True) -> str:
    if not isinstance(operations, list):
        return f"[Sandbox JSON Error] Expected list of operations, got {type(operations)}"
    
    if not operations:
        return "No operations to perform."

    from pathlib import Path
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    
    # Pre-flight
    replace_map = {}
    for op in operations:
        src = op.get("source_entity")
        tgt = op.get("target_entity")
        if not src or not tgt:
            return "Error: Each operation must have source_entity and target_entity."
        if src.casefold().endswith('.md'):
            src = src[:-3]
        if tgt.casefold().endswith('.md'):
            tgt = tgt[:-3]
        
        src_path = (wiki_dir / f"{src}.md").resolve()
        tgt_path = (wiki_dir / f"{tgt}.md").resolve()
        if not src_path.is_relative_to(wiki_dir) or not tgt_path.is_relative_to(wiki_dir):
            return f"[Security Error] Source '{src}' or target '{tgt}' resolves outside wiki directory."
        
        replace_map[src] = tgt

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
        return f"[DRY RUN] Validated {len(operations)} operations. No cycles detected. Would enqueue {len(operations)} merge tasks to the governance queue."

    # Enqueue to governance queue
    enqueued = 0
    now_str = datetime.now(timezone.utc).isoformat()
    
    for src, tgt in replace_map.items():
        # Prevent duplicating items
        item = {
            "item_id": f"gov_{uuid.uuid4().hex[:12]}",
            "type": "merge",
            "title": f"Merge {src} into {tgt}",
            "description": f"Bulk reconcile tool requested merge of {src} into {tgt}.",
            "created_at": now_str,
            "status": "pending",
            "source": "bulk_reconcile",
            "merge_source": src,
            "merge_target": tgt,
            "affected_pages": [f"{src}.md", f"{tgt}.md"],
            "merge_candidate": {
                "left_name": tgt,
                "right_name": src,
                "left_entity_id": f"entity_{tgt}",
                "right_entity_id": f"entity_{src}"
            }
        }
        if governance_store.insert_governance_item_if_absent(
            item,
            ("merge_source", "merge_target"),
        ):
            enqueued += 1

    return f"Success: Enqueued {enqueued} merge suggestions to the governance queue. Awaiting Mentat review."
