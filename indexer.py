import os
import json
import re
import yaml
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-indexer")

WIKI_DIR = os.path.expanduser("~/.gemini/MEMORY/wiki")

VALID_PREFIXES = ("Concept_", "Source_", "Entity_", "Synthesis_", "Event_", "Person_", "Project_", "Term_", "System_")


def _parse_wiki_node(filepath: str, node_key: str):
    """Parse a single wiki markdown file and extract structured metadata.
    Returns (node_data, aliases_list, categories_list) on success, or None on failure.
    Raises yaml.YAMLError if YAML parsing fails.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (UnicodeDecodeError, OSError) as e:
        log.warning(f"Cannot read {os.path.basename(filepath)}: {e}")
        return None

    frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
    if not frontmatter_match:
        return None

    fm_str = frontmatter_match.group(1)
    fm_data = yaml.safe_load(fm_str) or {}

    node_id = fm_data.get('id', "")
    title = fm_data.get('title', node_key)
    node_type = fm_data.get('type', "concept")
    updated = str(fm_data.get('updated', ""))
    cats = fm_data.get('categories', [])

    # --- Strict Mode Enforcement ---
    domain = fm_data.get('domain')
    topic_cluster = fm_data.get('topic_cluster', 'General')
    status = fm_data.get('status')

    if not domain or not status:
        log.warning(f"Schema violation: Missing 'domain' or 'status' in {os.path.basename(filepath)}. Node excluded from index.")
        return None

    raw_aliases = fm_data.get('aliases', [])
    aliases = []
    if isinstance(raw_aliases, list):
        aliases = [str(a).strip() for a in raw_aliases]
    elif isinstance(raw_aliases, str):
        aliases = [raw_aliases.strip()]

    # Extract [[bidirectional links]] from content body
    links = set()
    body = content[frontmatter_match.end():]
    # V7.0 relation links: [Relation:: [[Target]]]
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

    node_data = {
        "id": node_id,
        "title": title,
        "type": node_type,
        "updated": updated,
        "categories": cats,
        "domain": domain,
        "topic_cluster": topic_cluster,
        "status": status,
        "aliases": aliases,
        "links": sorted(links),
        "summary": summary
    }

    return node_data


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
        
        if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
            log.warning(f"Schema violation in {filename}, bypassing index inclusion.")
            continue
            
        node_key = filename[:-3] # remove .md
        
        try:
            node_data = _parse_wiki_node(filepath, node_key)
        except yaml.YAMLError as e:
            index_data["error_log"].append({"file": filename, "error": str(e)})
            log.warning(f"YAML Error in {filename}, suppressing to error_log.")
            continue

        if node_data is None:
            continue

        # Register in nodes
        index_data["nodes"][node_key] = node_data

        # Register aliases & id mapping back to node_key
        node_id = node_data["id"]
        if node_id:
            index_data["aliases"][node_id] = node_key
        index_data["aliases"][node_key] = node_key
        for al in node_data["aliases"]:
            index_data["aliases"][al] = node_key
            
        cats = node_data["categories"]
        if isinstance(cats, list):
            for c in cats:
                index_data["categories"].add(c)
                
    # Convert sets to list for JSON serialization
    index_data["categories"] = list(index_data["categories"])
    
    output_path = os.path.join(WIKI_DIR, "index.json")
    temp_path = output_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, output_path)
        
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
    
    if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
        if "error_log" not in index_data:
            index_data["error_log"] = []
        index_data["error_log"] = [e for e in index_data["error_log"] if e.get("file") != filename]
        index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
        log.warning(f"Schema violation in {filename} during partial update.")
        
        node_key = filename[:-3]
        if node_key in index_data.get("nodes", {}):
            del index_data["nodes"][node_key]
            
        temp_path = output_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, output_path)
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
        try:
            node_data = _parse_wiki_node(filepath, node_key)
        except yaml.YAMLError as e:
            index_data["error_log"].append({"file": filename, "error": str(e)})
            log.warning(f"YAML Error in {filename} during partial update.")
            if node_key in index_data.get("nodes", {}):
                del index_data["nodes"][node_key]
            node_data = None

        if node_data is None:
            index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing 'domain' or 'status'. Node excluded."})
            if node_key in index_data.get("nodes", {}):
                del index_data["nodes"][node_key]
        else:
            index_data["nodes"][node_key] = node_data
            
            node_id = node_data["id"]
            if node_id: index_data["aliases"][node_id] = node_key
            index_data["aliases"][node_key] = node_key
            for al in node_data["aliases"]:
                index_data["aliases"][al] = node_key
            
            cats = node_data["categories"]
            if isinstance(cats, list):
                cat_set = set(index_data.get("categories", []))
                for c in cats: cat_set.add(c)
                index_data["categories"] = list(cat_set)

    temp_path = output_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, output_path)

if __name__ == "__main__":
    generate_index()
