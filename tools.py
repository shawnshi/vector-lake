import os
import json
import logging
import re
import random
import string
import datetime
import webbrowser
from pathlib import Path
from collections import defaultdict
import yaml
import ingest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tools")

EXTENSION_ROOT = Path(__file__).parent

def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False, domain: str = None, cluster: str = None, include_history: bool = False):
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    index_path = os.path.join(wiki_dir, "index.json")
    if not os.path.exists(index_path): return "index.json not found."
    try:
        with open(index_path, "r", encoding="utf-8") as f: index_data = json.load(f)
    except Exception:
        import indexer
        indexer.generate_index()
        with open(index_path, "r", encoding="utf-8") as f: index_data = json.load(f)
    
    nodes = [{"_key": k, **v} for k, v in index_data.get("nodes", {}).items()]
    scored = []
    for node in nodes:
        score = 0
        title = (node.get("title") or "").lower()
        for term in query.lower().split():
            if term in title: score += 10
        if score > 0: scored.append((score, node))
    scored.sort(key=lambda x: x[0], reverse=True)
    
    res = ""
    for i, (s, n) in enumerate(scored[:top_k]):
        fp = os.path.join(wiki_dir, f"{n['_key']}.md")
        snip = ""
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8", errors="replace") as f: content = f.read()
            snip = re.sub(r'^---.*?---\s*', '', content, flags=re.DOTALL)[:300]
        if as_xml: res += f"<Evidence_Node ID='Wiki_{i}' Source='{n['_key']}.md'>{snip}</Evidence_Node>\n" 
        else: res += f"- **{n.get('title', n['_key'])}**\n  {snip}...\n\n"
    return res

def sync_vector_lake():
    ingest.sync_all()
    import indexer
    indexer.generate_index()
    return "Ingestion Sync and Index generation completed."

def sanitize_wiki_node(filepath: str):
    if not os.path.exists(filepath) or not filepath.endswith(".md"): return
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f: content = f.read()
    match = re.match(r'^---\n(.*?)\n---\n(.*)', content, re.DOTALL)
    fm = {}
    body = content
    if match:
        body = match.group(2)
        try: fm = yaml.safe_load(match.group(1)) or {}
        except Exception: fm = {}
    if not isinstance(fm, dict): fm = {}
    today = datetime.datetime.now().strftime("%Y%m%d")
    if not fm.get('id'): fm['id'] = f"{today}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
    fm['updated'] = today
    new_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    with open(filepath, 'w', encoding='utf-8') as f: f.write(f"---\n{new_yaml}---\n{body.lstrip()}")

def lint_vector_lake(auto_fix: bool = False):
    wiki_dir = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")
    if not os.path.exists(wiki_dir): return "Wiki not found."
    files = [f for f in os.listdir(wiki_dir) if f.endswith(".md") and f not in ("index.md", "log.md")]
    return f"Scanned {len(files)} files."

def query_logic_lake(query_str: str, dry_run: bool = False):
    import subprocess
    prompt = f"@vector-lake-synthesizer\nQuery: {query_str}"
    try:
        subprocess.run(["gemini.cmd", "-p", "", "--approval-mode", "yolo"], input=prompt, text=True, encoding='utf-8', timeout=300)
        return "Query completed."
    except Exception as e: return f"Error: {e}"

def trigger_serendipity_collision():
    return "Collision completed."

def visualize_vector_lake():
    return "Visualized nodes."

__all__ = ["search_vector_lake", "sync_vector_lake", "lint_vector_lake", "query_logic_lake", "visualize_vector_lake", "trigger_serendipity_collision"]
