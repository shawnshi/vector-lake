import logging
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import governance_store
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import get_wiki_dir, VALID_PREFIXES

log = logging.getLogger("governance_service")

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def resolve_governance_item(item_id: str, resolution: str = "skip", change_manifest: dict = None) -> dict | None:
    queue = governance_store.load_governance_queue()
    for item in queue["items"]:
        if item.get("item_id") != item_id:
            continue
        # We allow re-resolving if it's already resolved but we need to re-apply the merge
        if item.get("status") != "pending" and not (item.get("status") == "resolved" and resolution == "merge"):
            continue
        
        # ACTUALLY PERFORM THE MERGE LOGIC HERE IF RESOLUTION IS MERGE
        if resolution == "merge" and item.get("type") == "merge":
            candidate = item.get("merge_candidate")
            if candidate:
                left_id = candidate.get("left_entity_id")
                right_id = candidate.get("right_entity_id")
                if left_id and right_id:
                    left_name = candidate.get("left_name")
                    right_name = candidate.get("right_name")
                    left_path, right_path = None, None
                    old_left_entity = governance_store.get_entity(left_id)
                    
                    if left_name and right_name:
                        wiki_dir = get_wiki_dir().resolve()

                        def find_md_file(name):
                            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                                return None
                            for candidate_name in [
                                *(f"{prefix}{name}.md" for prefix in VALID_PREFIXES),
                                f"{name}.md",
                            ]:
                                path = (wiki_dir / candidate_name).resolve()
                                if path.parent == wiki_dir and path.exists():
                                    return path
                            return None
                            
                        left_path = find_md_file(left_name)
                        right_path = find_md_file(right_name)

                    # Validate every manifest constraint before canonical state changes.
                    if change_manifest:
                        try:
                            if old_left_entity and old_left_entity.get("merged_into") == right_id:
                                raise ValueError("Cycle detected: Target is already merged into Source.")
                            if change_manifest.get("allow_cycles", False) is False:
                                visited = set()
                                current = right_id
                                while current:
                                    if current in visited:
                                        raise ValueError("AHE Contract Failed: Alias cycle detected.")
                                    visited.add(current)
                                    current = governance_store.get_alias(current)
                        except Exception as e:
                            log.error(f"AHE Contract Failed before mutation: {e}.")
                            raise RuntimeError(f"Manifest validation failed: {e}") from e

                    if not left_path or not right_path or left_path == right_path:
                        raise RuntimeError("Semantic merge requires two distinct existing wiki pages.")

                    left_content = Path(left_path).read_text(encoding="utf-8")
                    right_content = Path(right_path).read_text(encoding="utf-8")
                    merged_content = merge_markdown_content(left_content, right_content)

                    def update_merge_registry():
                        governance_store.upsert_alias(right_id, left_id)

                    execute_mutation_batch(
                        [
                            {"filename": Path(left_path).name, "content": merged_content},
                            {"filename": Path(right_path).name, "is_delete": True},
                        ],
                        canonical_callback=update_merge_registry,
                    )

        from filelock import FileLock
        from vector_lake.wiki_utils import get_meta_dir
        lock_path = str(get_meta_dir() / "governance_queue.lock")
        with FileLock(lock_path, timeout=10):
            current_queue = governance_store.load_governance_queue()
            for q_item in current_queue.get("items", []):
                if q_item.get("item_id") == item_id:
                    q_item["status"] = "resolved"
                    q_item["resolution"] = resolution
                    q_item["resolved_at"] = _utc_now()
                    item = q_item
                    break
            governance_store.save_governance_queue(current_queue)
        return item
    return None
