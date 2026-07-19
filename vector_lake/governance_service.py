import hashlib
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
    item = governance_store.get_governance_item(item_id)
    if item is not None:
        # We allow re-resolving if it's already resolved but we need to re-apply the merge
        if item.get("status") != "pending" and not (item.get("status") == "resolved" and resolution == "merge"):
            return None
        
        # ACTUALLY PERFORM THE MERGE LOGIC HERE IF RESOLUTION IS MERGE
        if resolution == "merge" and item.get("type") == "merge":
            candidate = item.get("merge_candidate")
            if not isinstance(candidate, dict) or not candidate:
                raise RuntimeError(
                    "Missing merge candidate must be regenerated before resolution."
                )
            if candidate:
                required_contract_fields = (
                    "left_entity_id",
                    "right_entity_id",
                    "left_name",
                    "right_name",
                    "left_page_key",
                    "right_page_key",
                    "left_version",
                    "right_version",
                    "left_projection_hash",
                    "right_projection_hash",
                )
                if (
                    candidate.get("decision") != "merge"
                    or candidate.get("preflight_state") != "passed"
                    or any(not candidate.get(field) for field in required_contract_fields)
                ):
                    raise RuntimeError(
                        "Legacy or incomplete merge candidate must be regenerated "
                        "with decision, preflight, canonical versions, and projection hashes."
                    )
                left_id = candidate.get("left_entity_id")
                right_id = candidate.get("right_entity_id")
                if left_id and right_id:
                    left_name = candidate.get("left_name")
                    right_name = candidate.get("right_name")
                    left_page_key = candidate.get("left_page_key")
                    right_page_key = candidate.get("right_page_key")
                    left_path, right_path = None, None
                    old_left_entity = governance_store.get_entity(left_id)
                    
                    if left_name and right_name:
                        wiki_dir = get_wiki_dir().resolve()

                        def find_md_file(page_key, name):
                            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                                return None
                            candidate_names = [
                                f"{page_key}.md" if page_key else "",
                                *(f"{prefix}{name}.md" for prefix in VALID_PREFIXES),
                                f"{name}.md",
                            ]
                            for candidate_name in candidate_names:
                                if not candidate_name:
                                    continue
                                path = (wiki_dir / candidate_name).resolve()
                                if path.parent == wiki_dir and path.exists():
                                    return path
                            return None
                            
                        left_path = find_md_file(left_page_key, left_name)
                        right_path = find_md_file(right_page_key, right_name)

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

                    left_bytes = Path(left_path).read_bytes()
                    right_bytes = Path(right_path).read_bytes()
                    left_projection_hash = hashlib.sha256(left_bytes).hexdigest()
                    right_projection_hash = hashlib.sha256(right_bytes).hexdigest()
                    if (
                        left_projection_hash != candidate["left_projection_hash"]
                        or right_projection_hash != candidate["right_projection_hash"]
                    ):
                        raise RuntimeError(
                            "Markdown projection changed after merge preflight; regenerate the candidate."
                        )
                    left_content = left_bytes.decode("utf-8")
                    right_content = right_bytes.decode("utf-8")
                    merged_content = merge_markdown_content(
                        left_content,
                        right_content,
                        source_key=right_page_key or right_name,
                    )

                    def update_merge_registry():
                        governance_store.upsert_alias(right_id, left_id)

                    target_mutation = {
                        "filename": Path(left_path).name,
                        "content": merged_content,
                    }
                    source_mutation = {
                        "filename": Path(right_path).name,
                        "is_delete": True,
                    }
                    if "left_version" in candidate:
                        target_mutation["expected_version"] = candidate["left_version"]
                    if "right_version" in candidate:
                        source_mutation["expected_version"] = candidate["right_version"]

                    execute_mutation_batch(
                        [target_mutation, source_mutation],
                        canonical_callback=update_merge_registry,
                        # Reconciliation is bounded legacy maintenance: existing pages
                        # may predate the purpose contract while still satisfying the
                        # canonical Wiki schema required for a safe merge.
                        validation_mode="schema",
                    )

        return governance_store.update_governance_item(
            item_id,
            {
                "status": "resolved",
                "resolution": resolution,
                "resolved_at": _utc_now(),
            },
            expected_statuses={"pending", "resolved"} if resolution == "merge" else {"pending"},
        )
    return None
