import os
import json
import re
import yaml
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-indexer")

WIKI_DIR = os.path.expanduser("~/.gemini/MEMORY/wiki")

def generate_index():
    """Generates a lightweight index.json mapping from the wiki markdown files."""
    if not os.path.exists(WIKI_DIR):
        log.warning(f"Wiki directory not found at {WIKI_DIR}")
        return

    index_data = {
        "nodes": {},     # Metadata by filename (without .md)
        "aliases": {},   # Mapping of alias/id to filename
        "categories": set(),
        "error_log": []
    }

    files = [f for f in os.listdir(WIKI_DIR) if f.endswith(".md") and f not in ("index.md", "log.md")]
    
    for filename in files:
        filepath = os.path.join(WIKI_DIR, filename)
        
        valid_prefixes = ("Concept_", "Source_", "Entity_", "Event_", "Person_", "Project_", "Term_", "System_")
        if not filename.startswith(valid_prefixes) and filename not in ("index.md", "log.md"):
            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
            log.warning(f"Schema violation in {filename}, bypassing index inclusion.")
            continue
            
        node_key = filename[:-3] # remove .md
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except (UnicodeDecodeError, OSError) as e:
            index_data["error_log"].append({"file": filename, "error": f"Read error: {e}"})
            log.warning(f"Cannot read {filename}: {e}")
            continue

        frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
        if not frontmatter_match:
            continue
            
        fm_str = frontmatter_match.group(1)
        try:
            fm_data = yaml.safe_load(fm_str) or {}
        except yaml.YAMLError as e:
            index_data["error_log"].append({"file": filename, "error": str(e)})
            log.warning(f"YAML Error in {filename}, suppressing to error_log.")
            continue

        node_id = fm_data.get('id', "")
        title = fm_data.get('title', node_key)
        node_type = fm_data.get('type', "concept")
        updated = str(fm_data.get('updated', ""))
        cats = fm_data.get('categories', [])
        
        raw_aliases = fm_data.get('aliases', [])
        aliases = []
        if isinstance(raw_aliases, list):
            aliases = [str(a).strip() for a in raw_aliases]
        elif isinstance(raw_aliases, str):
            aliases = [raw_aliases.strip()]

        # Extract [[bidirectional links]] from content body
        links = set()
        body = content[frontmatter_match.end():]
        # V6.0 relation links: [Relation:: [[Target]]]
        for m in re.finditer(r'\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]', body):
            links.add(m.group(2).split('|')[0].strip().replace('.md', ''))
        # Standard wiki links: [[Target]] or [[Target|Alias]]
        for m in re.finditer(r'\[\[(.*?)\]\]', body):
            link_text = m.group(1).split('|')[0].strip().replace('.md', '')
            if '::' in link_text:
                link_text = link_text.split('::', 1)[1].strip()
            links.add(link_text)
        links.discard('')  # Remove empty strings

        # Extract summary (first 200 chars of clean body text)
        summary_text = re.sub(r'#.*?\n', '', body)  # Remove headings
        summary_text = re.sub(r'\[\[([^\]]*?\|)?([^\]]*?)\]\]', r'\2', summary_text)  # Clean wiki links
        summary_text = re.sub(r'\[([^\[\]]+?)::\s*\[\[.*?\]\]\]', '', summary_text)  # Remove relation links
        summary_text = summary_text.strip().replace('\n', ' ')
        summary = summary_text[:200]

        # Register in nodes
        index_data["nodes"][node_key] = {
            "id": node_id,
            "title": title,
            "type": node_type,
            "updated": updated,
            "categories": cats,
            "aliases": aliases,
            "links": sorted(links),
            "summary": summary
        }

        # Register aliases & id mapping back to node_key
        if node_id:
            index_data["aliases"][node_id] = node_key
        # Filename acts as alias
        index_data["aliases"][node_key] = node_key
        for al in aliases:
            index_data["aliases"][al] = node_key
            
        if isinstance(cats, list):
            for c in cats:
                index_data["categories"].add(c)
                
    # Convert sets to list for JSON serialization
    index_data["categories"] = list(index_data["categories"])
    
    output_path = os.path.join(WIKI_DIR, "index.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
        
    log.info(f"Generated index.json with {len(index_data['nodes'])} nodes | {len(index_data.get('error_log', []))} errors.")
    return output_path

def update_index_item(filename: str):
    """O(1) partial update for a single file in index.json."""
    if not filename.endswith(".md") or filename in ("index.md", "log.md"):
        return
        
    output_path = os.path.join(WIKI_DIR, "index.json")
    if not os.path.exists(output_path):
        return generate_index()
        
    with open(output_path, 'r', encoding='utf-8') as f:
        try:
            index_data = json.load(f)
        except json.JSONDecodeError:
            return generate_index()
            
    filepath = os.path.join(WIKI_DIR, filename)
    
    valid_prefixes = ("Concept_", "Source_", "Entity_", "Event_", "Person_", "Project_", "Term_", "System_")
    if not filename.startswith(valid_prefixes) and filename not in ("index.md", "log.md"):
        if "error_log" not in index_data:
            index_data["error_log"] = []
        index_data["error_log"] = [e for e in index_data["error_log"] if e.get("file") != filename]
        index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
        log.warning(f"Schema violation in {filename} during partial update.")
        
        node_key = filename[:-3]
        if node_key in index_data.get("nodes", {}):
            del index_data["nodes"][node_key]
            
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        return
        
    node_key = filename[:-3]
    
    # Prune old aliases mapping for this key
    index_data["aliases"] = {k: v for k, v in index_data.get("aliases", {}).items() if v != node_key}
    
    if "error_log" not in index_data:
        index_data["error_log"] = []
    index_data["error_log"] = [e for e in index_data["error_log"] if e.get("file") != filename]
    
    if not os.path.exists(filepath):
        # Was deleted
        if node_key in index_data.get("nodes", {}):
            del index_data["nodes"][node_key]
    else:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
        if frontmatter_match:
            fm_str = frontmatter_match.group(1)
            try:
                fm_data = yaml.safe_load(fm_str) or {}
                
                node_id = fm_data.get('id', "")
                title = fm_data.get('title', node_key)
                node_type = fm_data.get('type', "concept")
                updated = str(fm_data.get('updated', ""))
                cats = fm_data.get('categories', [])
                
                raw_aliases = fm_data.get('aliases', [])
                aliases = []
                if isinstance(raw_aliases, list):
                    aliases = [str(a).strip() for a in raw_aliases]
                elif isinstance(raw_aliases, str):
                    aliases = [raw_aliases.strip()]

                # Extract [[bidirectional links]] from content body
                links = set()
                body = content[frontmatter_match.end():]
                for m in re.finditer(r'\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]', body):
                    links.add(m.group(2).split('|')[0].strip().replace('.md', ''))
                for m in re.finditer(r'\[\[(.*?)\]\]', body):
                    link_text = m.group(1).split('|')[0].strip().replace('.md', '')
                    if '::' in link_text:
                        link_text = link_text.split('::', 1)[1].strip()
                    links.add(link_text)
                links.discard('')

                # Extract summary
                summary_text = re.sub(r'#.*?\n', '', body)
                summary_text = re.sub(r'\[\[([^\]]*?\|)?([^\]]*?)\]\]', r'\2', summary_text)
                summary_text = re.sub(r'\[([^\[\]]+?)::\s*\[\[.*?\]\]\]', '', summary_text)
                summary_text = summary_text.strip().replace('\n', ' ')
                summary = summary_text[:200]
                    
                index_data["nodes"][node_key] = {
                    "id": node_id,
                    "title": title,
                    "type": node_type,
                    "updated": updated,
                    "categories": cats,
                    "aliases": aliases,
                    "links": sorted(links),
                    "summary": summary
                }
                
                if node_id: index_data["aliases"][node_id] = node_key
                index_data["aliases"][node_key] = node_key
                for al in aliases:
                    index_data["aliases"][al] = node_key
                
                if isinstance(cats, list):
                    cat_set = set(index_data.get("categories", []))
                    for c in cats: cat_set.add(c)
                    index_data["categories"] = list(cat_set)
                    
            except yaml.YAMLError as e:
                index_data["error_log"].append({"file": filename, "error": str(e)})
                log.warning(f"YAML Error in {filename} during partial update.")
                if node_key in index_data["nodes"]:
                    del index_data["nodes"][node_key]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    generate_index()
