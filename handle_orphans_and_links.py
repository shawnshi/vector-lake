import os
import re
import logging
import yaml
from collections import defaultdict
from difflib import SequenceMatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("handle-orphans-links")

WIKI_DIR = "C:/Users/shich/.gemini/MEMORY/wiki"
ORPHAN_INDEX = os.path.join(WIKI_DIR, "Synthesis_Index_Orphans.md")

def read_markdown_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.startswith("---"):
            return {}, content, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content, content
        fm = yaml.safe_load(parts[1]) or {}
        body = parts[2]
        return fm, body, content
    except Exception as e:
        log.error(f"Error reading {path}: {e}")
        return None, None, None

def get_all_keys():
    files = [f for f in os.listdir(WIKI_DIR) if f.endswith(".md") and f not in ["index.md", "log.md"]]
    return {f[:-3]: f for f in files}

def fix_broken_links():
    all_keys = get_all_keys()
    alias_to_key = {}
    
    log.info("Building alias and title map...")
    for key, filename in all_keys.items():
        path = os.path.join(WIKI_DIR, filename)
        fm, _, _ = read_markdown_file(path)
        if fm:
            title = fm.get("title")
            if title:
                alias_to_key[str(title).strip()] = key
            aliases = fm.get("aliases", [])
            if isinstance(aliases, str): aliases = [aliases]
            if isinstance(aliases, list):
                for a in aliases:
                    alias_to_key[str(a).strip()] = key

    # Wikilink pattern: [[target|display]] or [[target]]
    link_pattern = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
    total_fixes = 0

    for filename in all_keys.values():
        path = os.path.join(WIKI_DIR, filename)
        fm, body, raw = read_markdown_file(path)
        if raw is None: continue
        
        def link_replacer(match):
            target = match.group(1).strip().replace(".md", "")
            display = match.group(2)
            
            if target in all_keys: return match.group(0)
            
            # 1. Try with prefixes
            for prefix in ["Concept_", "Source_", "Entity_", "Synthesis_"]:
                if prefix + target in all_keys:
                    return f"[[{prefix + target}|{display or target}]]"
            
            # 2. Try aliases/titles
            if target in alias_to_key:
                return f"[[{alias_to_key[target]}|{display or target}]]"
            
            # 3. Try variations (remove/add parenthetical suffixes)
            target_clean = re.sub(r"[\(_（\)].*$", "", target).strip()
            for k in all_keys:
                k_clean = re.sub(r"[\(_（\)].*$", "", k).strip()
                if target_clean == k_clean:
                    return f"[[{k}|{display or target}]]"

                # Check if target is a prefix of k
                if k.startswith(target):
                    return f"[[{k}|{display or target}]]"

                # Check if k starts with Concept_ + target
                for prefix in ["Concept_", "Entity_", "Source_"]:
                    if k.startswith(prefix + target):
                        return f"[[{k}|{display or target}]]"

            # 4. Fuzzy match
            best_match = None
            best_ratio = 0
            for k in all_keys:
                # Remove prefix for comparison
                k_disp = k.split("_", 1)[1] if "_" in k else k
                ratio = SequenceMatcher(None, target.lower(), k_disp.lower()).ratio()
                if ratio > 0.8: 
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = k

            if best_match:
                log.info(f"Fixed link in {filename}: [[{target}]] -> [[{best_match}]]")
                return f"[[{best_match}|{display or target}]]"

            return match.group(0)
        new_content = link_pattern.sub(link_replacer, raw)
        if new_content != raw:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            total_fixes += 1
            
    log.info(f"Fixed broken links in {total_fixes} files.")

def fix_orphans():
    all_keys = get_all_keys()
    inbound_count = defaultdict(int)
    
    link_pattern = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")
    
    for filename in all_keys.values():
        if filename == "Synthesis_Index_Orphans.md": continue
        path = os.path.join(WIKI_DIR, filename)
        _, _, raw = read_markdown_file(path)
        if raw:
            for match in link_pattern.finditer(raw):
                target = match.group(1).strip().replace(".md", "")
                if target in all_keys:
                    inbound_count[target] += 1
    
    orphans = []
    for key, filename in all_keys.items():
        if filename == "Synthesis_Index_Orphans.md": continue
        if filename.startswith("Source_"): continue 
        if inbound_count[key] == 0:
            orphans.append(key)
    
    if not orphans:
        log.info("No orphan pages found.")
        return

    log.info(f"Found {len(orphans)} orphan pages. Adding to {ORPHAN_INDEX}...")
    
    fm, body, raw = read_markdown_file(ORPHAN_INDEX)
    if raw:
        existing_links = set(re.findall(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", raw))
        new_links = []
        for o in orphans:
            if o not in existing_links:
                new_links.append(f"- [[{o}]]")
        
        if new_links:
            # Clean up the file if it's too large or has duplicates
            lines = raw.splitlines()
            cleaned_lines = []
            seen = set()
            for line in lines:
                m = re.search(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", line)
                if m:
                    link_target = m.group(1).strip()
                    if link_target in seen: continue
                    seen.add(link_target)
                cleaned_lines.append(line)
            
            # Append new ones
            for nl in new_links:
                cleaned_lines.append(nl)
                
            with open(ORPHAN_INDEX, "w", encoding="utf-8") as f:
                f.write("\n".join(cleaned_lines) + "\n")
            log.info(f"Added {len(new_links)} new orphan links.")
        else:
            log.info("All orphans already indexed.")
    else:
        # Create new orphan index if missing
        content = "---\ntitle: Orphan Pages Index\ntype: Synthesis\nstatus: active\n---\n\n"
        content += "\n".join([f"- [[{o}]]" for o in orphans])
        with open(ORPHAN_INDEX, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Created new orphan index with {len(orphans)} links.")

if __name__ == "__main__":
    fix_broken_links()
    fix_orphans()
