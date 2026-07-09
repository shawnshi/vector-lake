import os
import shutil
import sys
import subprocess
import logging
from datetime import datetime, timezone

from vector_lake import governance_store
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
                    left_bak, right_bak = None, None
                    left_path, right_path = None, None
                    
                    # AHE Phase 3: Snapshot state before mutation
                    old_registry = governance_store.load_alias_registry()
                    old_entities = governance_store.load_entities()
                    
                    if left_name and right_name:
                        wiki_dir = get_wiki_dir()
                        script_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "semantic_merge.py")
                        
                        def find_md_file(name):
                            for prefix in VALID_PREFIXES:
                                p = os.path.join(wiki_dir, f"{prefix}{name}.md")
                                if os.path.exists(p): return p
                            p = os.path.join(wiki_dir, f"{name}.md")
                            if os.path.exists(p): return p
                            return None
                            
                        left_path = find_md_file(left_name)
                        right_path = find_md_file(right_name)
                        
                        if left_path and os.path.exists(left_path):
                            left_bak = left_path + ".bak"
                            shutil.copy2(left_path, left_bak)
                        if right_path and os.path.exists(right_path):
                            right_bak = right_path + ".bak"
                            shutil.copy2(right_path, right_bak)
                            
                        if left_path and right_path and left_path != right_path and os.path.exists(script_path):
                            log.info(f"Triggering LLM semantic merge: {left_name} <- {right_name}")
                            env = os.environ.copy()
                            env["PYTHONIOENCODING"] = "utf-8"
                            try:
                                subprocess.run([sys.executable, script_path, left_path, right_path], env=env, check=True)
                            except subprocess.CalledProcessError as e:
                                if left_bak and os.path.exists(left_bak):
                                    shutil.move(left_bak, left_path)
                                if right_bak and os.path.exists(right_bak):
                                    shutil.move(right_bak, right_path)
                                raise RuntimeError(f"LLM Semantic Merge failed: {e}") from e

                    # Update the alias registry to map right to left
                    registry = governance_store.load_alias_registry()
                    registry["items"][right_id] = left_id
                    governance_store.save_alias_registry(registry)
                    
                    # Update entities file to mark right_id as merged/deprecated
                    entities = governance_store.load_entities()
                    if right_id in entities["items"]:
                        entities["items"][right_id]["status"] = "Merged"
                        entities["items"][right_id]["merged_into"] = left_id
                        governance_store.save_entities(entities)

                    # AHE Phase 3: Manifest Validation & Revert
                    if change_manifest:
                        try:
                            # Verify expectations
                            if old_entities["items"].get(left_id, {}).get("merged_into") == right_id:
                                raise ValueError("Cycle detected: Target is already merged into Source.")
                            
                            # Verify against manifest thresholds (example: max expected dead links)
                            if change_manifest.get("allow_cycles", False) is False:
                                # Quick cycle check in registry
                                visited = set()
                                current = right_id
                                while current in registry["items"]:
                                    if current in visited:
                                        raise ValueError("AHE Contract Failed: Alias cycle detected.")
                                    visited.add(current)
                                    current = registry["items"][current]
                        except Exception as e:
                            log.error(f"AHE Contract Failed: {e}. Executing ROLLBACK.")
                            
                            r = governance_store.load_alias_registry()
                            if right_id in old_registry["items"]:
                                r["items"][right_id] = old_registry["items"][right_id]
                            elif right_id in r["items"]:
                                del r["items"][right_id]
                            governance_store.save_alias_registry(r)
                            
                            e_store = governance_store.load_entities()
                            if right_id in old_entities["items"]:
                                e_store["items"][right_id] = old_entities["items"][right_id]
                            elif right_id in e_store["items"]:
                                del e_store["items"][right_id]
                            governance_store.save_entities(e_store)
                            
                            if left_bak and os.path.exists(left_bak):
                                shutil.move(left_bak, left_path)
                            if right_bak and os.path.exists(right_bak):
                                shutil.move(right_bak, right_path)
                                
                            raise RuntimeError(f"Manifest validation failed: {e}") from e

                    if left_bak and os.path.exists(left_bak):
                        os.remove(left_bak)
                    if right_bak and os.path.exists(right_bak):
                        os.remove(right_bak)

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
