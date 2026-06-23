import os
import json
import re
import logging
from typing import Dict, List, Any
from vector_lake.wiki_utils import get_wiki_dir, atomic_write_text

def bulk_reconcile(payload: str) -> str:
    try:
        data = json.loads(payload)
    except Exception as e:
        return f"[Sandbox JSON Error] Failed to parse payload: {e}"
    
    dry_run = data.get("dry_run", False)
    operations = data.get("operations", [])
    
    if not operations:
        return "No operations to perform."

    wiki_dir = get_wiki_dir()
    md_files = [f for f in os.listdir(wiki_dir) if f.endswith('.md')]
    
    # Pre-flight
    replace_map = {}
    for op in operations:
        src = op.get("source_entity")
        tgt = op.get("target_entity")
        if not src or not tgt:
            return "Error: Each operation must have source_entity and target_entity."
        # Strip .md if provided
        if src.endswith('.md'): src = src[:-3]
        if tgt.endswith('.md'): tgt = tgt[:-3]
        
        replace_map[src] = tgt

    # Check cycles
    for k, v in replace_map.items():
        curr = v
        visited = {k}
        while curr in replace_map:
            if curr in visited:
                return f"Error: Circular reference detected involving {curr}."
            visited.add(curr)
            curr = replace_map[curr]

    if dry_run:
        return f"[DRY RUN] Validated {len(operations)} operations. No cycles detected. Would modify {len(md_files)} files."

    # Execute file merges
    merged_count = 0
    for op in operations:
        action = op.get("action", "merge")
        src = op.get("source_entity")
        tgt = op.get("target_entity")
        if src.endswith('.md'): src = src[:-3]
        if tgt.endswith('.md'): tgt = tgt[:-3]
        
        src_path = os.path.join(wiki_dir, f"{src}.md")
        tgt_path = os.path.join(wiki_dir, f"{tgt}.md")

        if os.path.exists(src_path):
            if os.path.exists(tgt_path) and action == "merge":
                # Merge content
                with open(src_path, 'r', encoding='utf-8') as sf, open(tgt_path, 'a', encoding='utf-8') as tf:
                    content = sf.read()
                    content = re.sub(r'^---\n.*?\n---\n', '', content, flags=re.DOTALL)
                    tf.write("\n\n> [!NOTE]\n> 已合并关联实体。\n\n" + content)
            elif not os.path.exists(tgt_path):
                # Rename
                os.rename(src_path, tgt_path)
                continue
            
            # Delete src if not renamed
            try:
                os.remove(src_path)
            except Exception as e:
                logging.error(f"Failed to remove {src_path}: {e}")
            merged_count += 1

    # Execute link replacements globally
    pattern = re.compile(r'\[\[(.*?)\]\]')
    updated_files = 0
    
    for filename in md_files:
        file_path = os.path.join(wiki_dir, filename)
        if not os.path.exists(file_path):
            continue
            
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        def repl(match):
            inner = match.group(1)
            if '|' in inner:
                link_target, alias = inner.split('|', 1)
                if link_target in replace_map:
                    return f"[[{replace_map[link_target]}|{alias}]]"
            else:
                if inner in replace_map:
                    # Convert [[Source]] to [[Target|Source]]
                    return f"[[{replace_map[inner]}|{inner}]]"
            return match.group(0)
            
        new_content = pattern.sub(repl, content)
        if new_content != content:
            atomic_write_text(file_path, new_content)
            updated_files += 1

    return f"Success: Merged {merged_count} source files and updated links in {updated_files} files."
