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
---
# {title}

## 1. 编译事实 (Compiled Truth - READ MODEL)
*[System Directive: This section represents the LATEST consensus. NO historical narrative here. NO marketing fluff.]*

## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)
*[System Directive: Append-only ledger of historical state changes and events.]*
"""

    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target_path, scaffold)
        
    with open(target_path, "r", encoding="utf-8") as f:
        file_content = f.read()
        
    # Append to the end of the file (which falls under the Timeline section)
    # Ensure it ends with a newline before appending
    if not file_content.endswith("\n"):
        file_content += "\n"
        
    # Format the memory entry
    clean_content = content.strip().replace("\n", "  \n") # preserve internal newlines
    new_entry = f"- [{now_str}] {clean_content} (Source: [[Operational_Memory]])\n"
    
    file_content += new_entry
    
    atomic_write_text(target_path, file_content)
    
    return f"Successfully persisted {memory_type} to {filename}."

