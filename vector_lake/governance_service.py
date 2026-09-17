import logging
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import governance_metrics, governance_store
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import get_wiki_dir, VALID_PREFIXES

log = logging.getLogger("governance_service")

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _mark_resolved(item: dict, resolution: str) -> None:
    item["status"] = "resolved"
    item["resolution"] = resolution
    item["resolved_at"] = _utc_now()


def resolve_governance_item(item_id: str, resolution: str = "skip", change_manifest: dict = None) -> dict | None:
    """Resolve one governance item, committing the item and its effect together.

    The queue lock is taken *before* the database transaction: the documented lock
    order is file-lock -> database transaction, and the merge path below writes the
    queue row inside the mutation's own transaction.  That is what stops a merge
    from committing while its item stays ``pending`` - a client or process that
    dies in between used to leave an item whose consumed page no longer exists, so
    re-running it could never succeed.
    """
    with governance_store.governance_queue_session():
        return _resolve_locked(item_id, resolution, change_manifest)


def _resolve_locked(item_id: str, resolution: str = "skip", change_manifest: dict = None) -> dict | None:
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

                    # Entry guard.  A shared name claimed by three or more live
                    # entities cannot be resolved by merging one pair, and callers
                    # other than find_merge_candidates (bulk_reconciliation) build
                    # their own merge_candidate, so the check has to sit here.
                    hazards = governance_metrics.ambiguous_name_hazards(
                        [Path(left_path).stem, Path(right_path).stem]
                    )
                    if hazards and not (change_manifest or {}).get("allow_ambiguous_names", False):
                        raise RuntimeError(
                            "Manifest validation failed: ambiguous name claim; "
                            + "; ".join(hazards)
                            + '. Re-issue with {"allow_ambiguous_names": true} to force.'
                        )

                    left_content = Path(left_path).read_text(encoding="utf-8")
                    right_content = Path(right_path).read_text(encoding="utf-8")
                    merged_content = merge_markdown_content(left_content, right_content)

                    def commit_resolution():
                        # One transaction with the canonical mutation: the registry
                        # alias and the queue row either both land or neither does.
                        governance_store.upsert_alias(right_id, left_id)
                        _mark_resolved(item, resolution)
                        governance_store.save_governance_queue(queue)

                    execute_mutation_batch(
                        [
                            {"filename": Path(left_path).name, "content": merged_content},
                            {"filename": Path(right_path).name, "is_delete": True},
                        ],
                        canonical_callback=commit_resolution,
                    )
                    return item

        _mark_resolved(item, resolution)
        governance_store.save_governance_queue(queue)
        return item
    return None
