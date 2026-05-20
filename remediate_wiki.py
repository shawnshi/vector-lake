import os
import re
import logging
import yaml
from collections import defaultdict
from difflib import SequenceMatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("remediate-wiki")

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

def remediate():
    wiki_dir = "C:/Users/shich/.gemini/MEMORY/wiki"
    files = [f for f in os.listdir(wiki_dir) if f.endswith(".md") and f not in ["index.md", "log.md"]]
    all_keys = {f[:-3]: f for f in files}
    alias_to_key = {}
    
    log.info(f"Scanning {len(files)} files for metadata...")
    file_data = {}
    for f in files:
        path = os.path.join(wiki_dir, f)
        fm, body, raw = read_markdown_file(path)
        if fm is None: continue
        file_data[f] = {"fm": fm, "body": body, "raw": raw, "path": path}
        
        # Build alias map
        aliases = fm.get("aliases", [])
        if isinstance(aliases, str): aliases = [aliases]
        if isinstance(aliases, list):
            for a in aliases:
                alias_to_key[str(a).strip()] = f[:-3]
        
        title = fm.get("title")
        if title:
            alias_to_key[str(title).strip()] = f[:-3]
            # Handle bilingual titles like "世界模型 (World Model)"
            if "(" in title:
                parts = re.split(r"[()（）]", title)
                for p in parts:
                    if p.strip(): alias_to_key[p.strip()] = f[:-3]

    log.info(f"Built alias map with {len(alias_to_key)} entries.")

    # Fix Broken Links
    link_pattern = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
    total_link_fixes = 0

    for f, data in file_data.items():
        content = data["raw"]
        
        def link_replacer(match):
            target = match.group(1).strip().replace(".md", "")
            display = match.group(2)
            
            # 1. Already valid?
            if target in all_keys: return match.group(0)
            
            # 2. Key with prefix? (e.g. Concept_Concept_...)
            clean_target = target.replace("Concept_Concept_", "Concept_").replace("Entity_Entity_", "Entity_")
            if clean_target in all_keys:
                return f"[[{clean_target}|{display or target}]]"

            # 3. Is it an alias?
            if target in alias_to_key:
                primary = alias_to_key[target]
                return f"[[{primary}|{display or target}]]"
            
            # 4. Fuzzy match for core concepts
            best_match = None
            best_ratio = 0
            for k in all_keys:
                # Prioritize startswith or high overlap
                ratio = SequenceMatcher(None, target.lower(), k.lower()).ratio()
                if ratio > 0.85:
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = k
            
            if best_match:
                log.info(f"Fuzzy match: [[{target}]] -> [[{best_match}]] in {f}")
                return f"[[{best_match}|{display or target}]]"
            
            return match.group(0)

        new_content = link_pattern.sub(link_replacer, content)
        if new_content != content:
            with open(data["path"], "w", encoding="utf-8") as out:
                out.write(new_content)
            total_link_fixes += 1

    log.info(f"Fixed {total_link_fixes} files with broken links.")

if __name__ == "__main__":
    remediate()
