import os
import json
import hashlib
import time
import logging
import concurrent.futures
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from threading import Lock

from db import get_chroma_client, get_processed_files, mark_file_processed
from embedding import GeminiEmbeddingFunction

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-ingest")

# Shared resources
db_lock = Lock()

# Load config
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

# Resolve paths
EXTENSION_ROOT = Path(__file__).parent
TARGET_DIRS = [str((EXTENSION_ROOT / d).resolve()) for d in config.get("target_directories", [])]
WIKI_DIR = (EXTENSION_ROOT / "../../MEMORY/wiki").resolve()
INDEX_PATH = WIKI_DIR / "index.md"
LOG_PATH = WIKI_DIR / "log.md"
SCHEMA_PATH = EXTENSION_ROOT / "schema.md"

SUPPORTED_EXTS = { ".md", ".txt", ".png", ".jpeg", ".pdf" }

def calculate_hash(filepath: str) -> str:
    hasher = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        log.error(f"Error calculating hash for {filepath}: {e}")
        return ""

def get_file_content_safely(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def index_wiki_page_in_chroma(filepath: str, chroma_collection):
    """Chunks and indexes a wiki page into ChromaDB using AST semantic chunking."""
    import re
    content = get_file_content_safely(filepath)
    if not content:
        return
        
    lines = content.split('\n')
    chunks = []
    current_chunk = []
    current_path = []
    
    def process_chunk(path, text):
        if not text.strip(): return
        prefix = " > ".join(path) if path else ""
        if prefix:
            prefix = f"[{prefix}]\n"
        chunks.append(prefix + text.strip())

    in_code_block = False
    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            
        if not in_code_block and re.match(r'^(#{1,6})\s+(.*)', line):
            if current_chunk:
                process_chunk(current_path, "\n".join(current_chunk))
                current_chunk = []
            
            match = re.match(r'^(#{1,6})\s+(.*)', line)
            level = len(match.group(1))
            title = match.group(2).strip()
            
            current_path = current_path[:level-1]
            while len(current_path) < level - 1:
                current_path.append("...")
            current_path.append(title)
            
            current_chunk.append(line)
        else:
            current_chunk.append(line)
            
    if current_chunk:
        process_chunk(current_path, "\n".join(current_chunk))

    final_chunks = []
    for c in chunks:
        if len(c) > 1500:
            subchunks = re.split(r'(\n\n)', c)
            temp = ""
            for part in subchunks:
                if len(temp) + len(part) > 1500 and temp:
                    final_chunks.append(temp.strip())
                    temp = part
                else:
                    temp += part
            if temp.strip():
                final_chunks.append(temp.strip())
        else:
            final_chunks.append(c)

    if final_chunks:
        ids = [f"{os.path.basename(filepath)}_{i}" for i in range(len(final_chunks))]
        metadatas = [{"source": filepath} for _ in final_chunks]
        with db_lock:
            try:
                chroma_collection.delete(where={"source": filepath})
            except Exception:
                pass
            chroma_collection.upsert(documents=final_chunks, metadatas=metadatas, ids=ids)

def process_file_batch(filepaths: list, chroma_collection):
    if not filepaths: return False

    log.info(f"Delegating ingestion of a batch of {len(filepaths)} files to Vector Lake Ingestor Agent...")
    
    before_mtimes = {}
    if os.path.exists(WIKI_DIR):
        for entry in os.scandir(WIKI_DIR):
            if entry.is_file() and entry.name.endswith('.md'):
                before_mtimes[entry.path] = entry.stat().st_mtime

    # Use relative paths to avoid encoding issues with absolute paths in the prompt
    root_dir = str(EXTENSION_ROOT.parent.parent.resolve())
    rel_filepaths = []
    for p in filepaths:
        try:
            rel_filepaths.append(os.path.relpath(p, root_dir))
        except ValueError:
            rel_filepaths.append(p)
            
    file_list_str = "\n".join([f"- {p}" for p in rel_filepaths])
    # The Subagent holds the schema, rules, and identity within its `system.md`.
    # We only need to provide the target paths.
    prompt = f"""
@vector-lake-ingestor
[BATCH INGEST PROCESS EXECUTED]
Please compile the following raw source files into the Wiki directory (`{WIKI_DIR}`):

Source Files:
{file_list_str}

Please begin extraction and node weaving.
"""

    gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    # Pivot to Native Subagent via @mention
    cmd = [gemini_exec, "--prompt", prompt, "--approval-mode", "yolo"]
    try:
        cmd = [gemini_exec, "--prompt", "", "--approval-mode", "yolo"]
        print("Waiting for Ingestor Agent to process the batch (this may take 1~2 minutes)...", flush=True)
        result = subprocess.run(cmd, input=prompt, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        
        if result.stdout: print(result.stdout)
        success = (result.returncode == 0)
    except Exception as e:
        log.error(f"Gemini CLI failed for batch: {e}")
        success = False

    if not success:
        log.error("Ingestor Subagent failed or crashed during batch processing.")
        return False
            
    changed_files = []
    if os.path.exists(WIKI_DIR):
        for entry in os.scandir(WIKI_DIR):
            if entry.is_file() and entry.name.endswith('.md'):
                p = entry.path
                mtime = entry.stat().st_mtime
                if p not in before_mtimes or mtime > before_mtimes[p]:
                    changed_files.append(p)
                    
    if changed_files:
        log.info(f"Agent modified {len(changed_files)} wiki files. Re-indexing...")
        for p in changed_files:
            if p.endswith(".md"):
                index_wiki_page_in_chroma(p, chroma_collection)
    else:
        log.warning("Agent ran for batch but no wiki files were modified.")

    now = datetime.now(timezone.utc).isoformat()
    for fp in filepaths:
        f_hash = calculate_hash(fp)
        if f_hash: mark_file_processed(fp, f_hash, now)
        
    return True

def sync_all():
    log.info("Starting Native Agent Ingest Sync...")
    chroma_client = get_chroma_client()
    embedding_func = GeminiEmbeddingFunction()
    
    collection = chroma_client.get_or_create_collection(
        name="vector_lake",
        embedding_function=embedding_func
    )
    
    files_to_process = []
    for target_dir in TARGET_DIRS:
        folder = Path(target_dir)
        if not folder.exists(): continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')] # Ignore hidden directories
            for file in files:
                if file.startswith('~') or file.startswith('.'): continue # Ignore temporary/hidden files
                if os.path.splitext(file)[1].lower() in SUPPORTED_EXTS:
                    files_to_process.append(os.path.join(root, file))

    log.info(f"Scanned {len(files_to_process)} candidate raw sources.")

    batch = []
    batch_size = 5
    
    for filepath in files_to_process:
        file_hash = calculate_hash(filepath)
        if not file_hash: continue
        processed = get_processed_files()
        if filepath in processed and processed[filepath].get("hash") == file_hash:
            continue
            
        batch.append(filepath)
        if len(batch) >= batch_size:
            process_file_batch(batch, collection)
            batch = []
            
    if batch:
        process_file_batch(batch, collection)
                    
    log.info("Sync completed.")

if __name__ == "__main__":
    sync_all()
