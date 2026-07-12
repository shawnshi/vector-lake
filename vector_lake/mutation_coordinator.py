import os
import json
import shutil
from pathlib import Path
import logging

from vector_lake.db_store import transaction, backup_database
from vector_lake.wiki_utils import get_wiki_dir, get_extension_root
from vector_lake.defense_hook import verify_asset
from vector_lake.indexer import update_index_items

log = logging.getLogger("vector-lake-mutation")

def execute_mutation_plan(filename: str, content: str | None = None, is_delete: bool = False):
    """
    Unified Mutation Coordinator for vector_lake.
    Pre-checks schema/purpose, performs atomic writes, SQLite transactions, and compensation.
    """
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / filename
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    bak_path = tmp_dir / f"{filename}.bak"
    
    # 1. Pre-flight schema/purpose validation (if not deleting)
    if not is_delete and content is not None:
        from vector_lake.wiki_utils import split_frontmatter
        fm, _ = split_frontmatter(content)
        # verify_asset raises DefenseHookException if invalid
        verify_asset(content, filename, fm, get_wiki_dir().parent / "index.json")
        
    # 2. Backup existing file
    has_backup = False
    if filepath.exists():
        shutil.copy2(filepath, bak_path)
        has_backup = True
        
    try:
        # 3. Markdown Write
        if is_delete:
            if filepath.exists():
                os.remove(filepath)
        else:
            # Write directly
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

            
        # 4. SQLite Transaction
        with transaction():
            from vector_lake.db_store import get_connection
            from vector_lake.governance_store import _utc_now
            conn = get_connection()
            if is_delete:
                from vector_lake.db_store import delete_node_cascade
                node_key = filename[:-3] if filename.endswith(".md") else filename
                delete_node_cascade(node_key)
                conn.execute("INSERT INTO mutation_outbox (filename, mutation_type, created_at) VALUES (?, ?, ?)", (filename, 'delete', _utc_now()))
            else:
                from vector_lake.governance_store import sync_pages_to_canonical
                sync_pages_to_canonical(
                    [str(filepath)],
                    origin="mutation_coordinator",
                    auto_approve=True,
                    summary=f"Unified mutation applied to {filename}"
                )
                conn.execute("INSERT INTO mutation_outbox (filename, mutation_type, created_at) VALUES (?, ?, ?)", (filename, 'update', _utc_now()))
                
        # 5. Signal Outbox Consumer
        tmp_dir = get_extension_root() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with open(tmp_dir / "outbox_signal.lock", "w") as f:
            f.write("1")
            
        # Cleanup backup
        if has_backup and bak_path.exists():
            os.remove(bak_path)
            
        return True, "Mutation completed successfully."
        
    except Exception as e:
        log.error(f"Mutation failed for {filename}: {e}. Rolling back.")
        # Rollback markdown
        if has_backup and bak_path.exists():
            if filepath.exists():
                os.remove(filepath)
            shutil.move(str(bak_path), str(filepath))
        elif not is_delete and not has_backup and filepath.exists():
            os.remove(filepath)
            
        raise e
