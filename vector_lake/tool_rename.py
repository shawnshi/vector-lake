import os
import re
from pathlib import Path
from vector_lake.wiki_utils import get_wiki_dir, normalize_entity_name, read_markdown_file, write_markdown_file

def rename_vector_lake_entity(old_name: str, new_name: str, dry_run: bool = True) -> str:
    """Safely renames an entity and updates all internal markdown links across the Wiki."""
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    
    if not old_name.endswith(".md"):
        old_name += ".md"
    if not new_name.endswith(".md"):
        new_name += ".md"
        
    old_path = (wiki_dir / old_name).resolve()
    if not old_path.is_relative_to(wiki_dir):
        return f"[Security Error] Old entity '{old_name}' is outside the wiki directory."
    
    # 1. Validation
    if not old_path.exists():
        return f"Error: Old entity '{old_name}' does not exist."
        
    normalized_new_name = normalize_entity_name(new_name[:-3]) + ".md"
    new_path = (wiki_dir / normalized_new_name).resolve()
    if not new_path.is_relative_to(wiki_dir):
        return f"[Security Error] Target entity '{normalized_new_name}' is outside the wiki directory."
    
import os
import re
from pathlib import Path
from vector_lake.wiki_utils import get_wiki_dir, normalize_entity_name, read_markdown_file, write_markdown_file

def rename_vector_lake_entity(old_name: str, new_name: str, dry_run: bool = True) -> str:
    """Safely renames an entity and updates all internal markdown links across the Wiki."""
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    
    if not old_name.endswith(".md"):
        old_name += ".md"
    if not new_name.endswith(".md"):
        new_name += ".md"
        
    old_path = (wiki_dir / old_name).resolve()
    if not old_path.is_relative_to(wiki_dir):
        return f"[Security Error] Old entity '{old_name}' is outside the wiki directory."
    
    # 1. Validation
    if not old_path.exists():
        return f"Error: Old entity '{old_name}' does not exist."
        
    normalized_new_name = normalize_entity_name(new_name[:-3]) + ".md"
    new_path = (wiki_dir / normalized_new_name).resolve()
    if not new_path.is_relative_to(wiki_dir):
        return f"[Security Error] Target entity '{normalized_new_name}' is outside the wiki directory."
    
    if new_path.exists():
        return f"Error: Target entity '{normalized_new_name}' already exists. Use merge instead."
        
    if dry_run:
        return f"[DRY-RUN] Would rename '{old_name}' to '{normalized_new_name}' and update links in other files."
        
    from filelock import FileLock
    from vector_lake.wiki_utils import get_meta_dir, atomic_write_text
    from vector_lake.governance_store import sync_pages_to_canonical
    from vector_lake.mutation_coordinator import execute_mutation_plan
    import yaml
    
    lock_path = str(get_meta_dir() / "governance_sync.lock")
    affected_pages = []
    
    with FileLock(lock_path, timeout=60):
        # 2. Rename the file and update its frontmatter
        try:
            frontmatter, body, _ = read_markdown_file(old_path)
            # Update title if it matches the old core name
            old_core = old_name.split("_", 1)[-1][:-3] if "_" in old_name else old_name[:-3]
            new_core = normalized_new_name.split("_", 1)[-1][:-3] if "_" in normalized_new_name else normalized_new_name[:-3]
            
            if frontmatter.get("title") == old_core:
                frontmatter["title"] = new_core
                
            # Add old core to aliases if not present
            aliases = frontmatter.get("aliases", [])
            if old_core not in aliases:
                aliases.append(old_core)
            frontmatter["aliases"] = aliases
            
            # Delete old file via mutation coordinator
            execute_mutation_plan(old_name, is_delete=True)
            
            # Create new file via mutation coordinator
            fm_str = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
            new_content = f"---\n{fm_str}---\n{body}"
            execute_mutation_plan(normalized_new_name, content=new_content, is_delete=False)
            
        except Exception as e:
            return f"Error during file rename: {str(e)}"
            
        # 3. Global Link Resolution
        old_display_name = old_core
        # Pattern to match [[old_name]] or [[old_name|...]] or [xxx:: [[old_name]]]
        # We must match the exact filename without extension
        old_filename_no_ext = old_name[:-3]
        new_filename_no_ext = normalized_new_name[:-3]
        
        # Regex to find [[OldName]] or [[OldName|Alias]]
        pattern_exact = re.compile(r'\[\[' + re.escape(old_filename_no_ext) + r'\]\]')
        pattern_with_alias = re.compile(r'\[\[' + re.escape(old_filename_no_ext) + r'\|([^\]]+)\]\]')
        
        updated_files = 0
        affected_filepaths = []
        for root, _, files in os.walk(wiki_dir):
            for file in files:
                if not file.endswith(".md") or file in ["index.md", "log.md", "overview.md"]:
                    continue
                    
                filepath = Path(root) / file
                if filepath == new_path:
                    continue
                    
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                        
                    new_content = content
                    # Replace [[Old_File]] -> [[New_File|Old_File]]
                    new_content = pattern_exact.sub(f'[[{new_filename_no_ext}|{old_display_name}]]', new_content)
                    # Replace [[Old_File|Alias]] -> [[New_File|Alias]]
                    new_content = pattern_with_alias.sub(r'[[' + new_filename_no_ext + r'|\1]]', new_content)
                    
                    if new_content != content:
                        atomic_write_text(filepath, new_content)
                        affected_filepaths.append(str(filepath))
                        updated_files += 1
                except Exception:
                    pass
                    
        # Batch sync the affected files
        if affected_filepaths:
            sync_pages_to_canonical(affected_filepaths, origin="tool_rename", auto_approve=True, summary=f"Updated links for rename of {old_name}")
            from vector_lake.indexer import update_index_items
            update_index_items([Path(f).name for f in affected_filepaths])
            
        return f"Successfully renamed '{old_name}' to '{normalized_new_name}'. Updated links in {updated_files} files."
