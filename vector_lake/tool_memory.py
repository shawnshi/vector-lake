import os
import re
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import get_wiki_dir, atomic_write_text

MEMORY_TYPE_MAP = {
    "preference": ("Concept_UserPreferences.md", "User Preferences"),
    "decision": ("Concept_SystemDecisions.md", "System Decisions"),
    "task_state": ("Concept_AgentTaskState.md", "Agent Task State"),
    "fact": ("Concept_OperationalFacts.md", "Operational Facts"),
}

def update_operational_memory(memory_type: str, content: str) -> str:
    """
    Safely persist an operational memory (preference, decision, fact, task_state)
    without corrupting the graph. Appends to the Evidence Timeline of the respective wiki page.
    """
    memory_type = memory_type.lower().strip()
    if memory_type not in MEMORY_TYPE_MAP:
        return f"Error: Invalid memory_type '{memory_type}'. Must be one of: {list(MEMORY_TYPE_MAP.keys())}"
    
    filename, title = MEMORY_TYPE_MAP[memory_type]
    target_path = get_wiki_dir() / filename
    
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Dual-schema scaffold
    scaffold = f"""---
title: {title}
type: concept
memory_type: {memory_type}
domain: system
status: active
sources:
  - Operational_Memory
strategic_scope: core
evidence_tier: derived
---
# {title}

## 1. 编译事实 (Compiled Truth - READ MODEL)
*[System Directive: This section represents the LATEST consensus. NO historical narrative here. NO marketing fluff.]*

## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)
*[System Directive: Append-only ledger of historical state changes and events.]*
"""

    from filelock import FileLock
    lock_path = str(target_path) + ".lock"
    with FileLock(lock_path, timeout=10):
        if not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target_path, scaffold)
            
        with open(target_path, "r", encoding="utf-8") as f:
            file_content = f.read()
            
        # Update the 'updated' field in frontmatter
        import re
        from vector_lake.governance_store import _utc_now
        file_content = re.sub(r'updated:.*?\n', f'updated: {_utc_now()}\n', file_content)
        if 'updated:' not in file_content and '---\n' in file_content:
            file_content = file_content.replace('---\n', f'---\nupdated: {_utc_now()}\n', 1)
            
        if not file_content.endswith("\n"):
            file_content += "\n"
            
        clean_content = content.strip().replace("\n", "  \n")
        new_entry = f"- [{now_str}] {clean_content} (Source: [[Operational_Memory]])\n"
        
        file_content += new_entry
        
        atomic_write_text(target_path, file_content)
    
    # Trigger synchronous index update to prevent MCP bypass
    try:
        from vector_lake.indexer import update_index_item
        update_index_item(filename)
        from vector_lake.governance_store import sync_pages_to_canonical
        sync_pages_to_canonical([str(target_path)], origin="mcp-memory", auto_approve=True, summary=f"MCP memory update to {filename}")
    except Exception as e:
        return f"Successfully persisted {memory_type} to {filename}, but failed to sync index: {e}"
        
    return f"Successfully persisted {memory_type} to {filename} and synced to Logic Lake."

