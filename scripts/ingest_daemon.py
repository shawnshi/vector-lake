import sys
import os
import json
import uuid
import time
from pathlib import Path

# Fix python import path for orchestrator
global_scripts_dir = Path(r"C:\Users\shich\.gemini\config\skills\scripts")
if str(global_scripts_dir) not in sys.path:
    sys.path.insert(0, str(global_scripts_dir))

try:
    from orchestrator import BasePipelineOrchestrator
except ImportError:
    print("Error: Could not import BasePipelineOrchestrator.")
    sys.exit(1)

# Add vector_lake to path
vl_dir = Path(r"C:\Users\shich\.gemini\config\plugins\vector-lake")
if str(vl_dir) not in sys.path:
    sys.path.insert(0, str(vl_dir))

from vector_lake.db import get_processed_files, mark_file_processed
from vector_lake.wiki_utils import get_wiki_dir, get_memory_dir
from vector_lake.tool_ingest import calculate_hash, canonical_source_name, _read_index_summary
from vector_lake import get_extension_root
from vector_lake import governance_store

class IngestDaemon(BasePipelineOrchestrator):
    def __init__(self):
        super().__init__("IngestDaemon")
        self.wiki_dir = get_wiki_dir()

    def process_pending(self):
        self.logger.info("Starting Ingest Daemon Async Queue processing...")
        config_path = get_extension_root() / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
            
        target_dirs = [str((get_extension_root() / d).resolve()) for d in config.get("target_directories", [])]
        exclude_paths = config.get("exclude_paths", [])
        supported_exts = set(config.get("supported_extensions", [".md", ".txt"]))
        
        files_to_process = []
        for target_dir in target_dirs:
            folder = Path(target_dir)
            if not folder.exists(): continue
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for file in files:
                    if file.startswith('~') or file.startswith('.'): continue
                    
                    filepath = os.path.join(root, file)
                    path_str = filepath.replace("\\", "/")
                    if any(exclude in path_str for exclude in exclude_paths):
                        continue

                    if os.path.splitext(file)[1].lower() in supported_exts:
                        files_to_process.append(filepath)
                        
        processed = get_processed_files()
        pending_files = []
        
        for filepath in files_to_process:
            file_hash = calculate_hash(filepath)
            if not file_hash: continue
            if filepath in processed and processed[filepath].get("hash") == file_hash:
                continue
            pending_files.append((filepath, file_hash))
            
        if not pending_files:
            self.logger.info("No new files to ingest. System is fully synced.")
            return

        self.logger.info(f"Found {len(pending_files)} files to ingest. Processing serially to avoid API EOF...")

        schema_content = ""
        try:
            schema_content = (get_extension_root() / "schema.md").read_text(encoding="utf-8")
        except Exception: pass
        
        for filepath, file_hash in pending_files:
            self.logger.info(f"Ingesting: {filepath}")
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
                
            canonical_name = canonical_source_name(filepath)
            
            prompt = f"""
            You are the Vector Lake Ingestion Engine.
            Your task is to ingest a raw source file into the Knowledge Graph (Wiki).
            
            Source Content:
            {content[:15000]} # Limit to avoid context bloat
            
            Wiki Rules & Schema:
            {schema_content}
            
            Existing Index Summary:
            {_read_index_summary()}
            
            Task:
            1. Extract the core entities, concepts, and tensions.
            2. Generate the Markdown content for the main source page (`{canonical_name}`).
            3. Generate the Markdown content for any newly discovered entity pages.
            
            Output your response STRICTLY in the following JSON format:
            {{
                "files": [
                    {{
                        "filename": "{canonical_name}",
                        "content": "# Extracted Source Content\\n..."
                    }},
                    {{
                        "filename": "Entity_ConceptName.md",
                        "content": "# ConceptName\\n..."
                    }}
                ]
            }}
            Do NOT include markdown block backticks around the JSON. Only pure JSON object.
            """
            
            self.logger.info(f"Calling LLM for {filepath}...")
            try:
                response = self.call_llm(prompt, system_instruction="You are an automated ETL pipeline. Output only valid JSON.")
                
                response = response.strip()
                if response.startswith("```json"): response = response.split("```json", 1)[1]
                if response.endswith("```"): response = response.rsplit("```", 1)[0]
                
                data = json.loads(response.strip())
                files_written = []
                for item in data.get("files", []):
                    fname = item["filename"]
                    fcontent = item["content"]
                    out_path = self.wiki_dir / fname
                    self.write_file(str(out_path), fcontent)
                    files_written.append(str(out_path))
                    self.logger.info(f"Wrote file: {out_path}")
                
                if files_written:
                    governance_store.sync_pages_to_canonical(
                        files_written,
                        origin="ingest-daemon",
                        auto_approve=True,
                        summary=f"Daemon ingest sync for {len(files_written)} page(s)"
                    )
                    
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                mark_file_processed(filepath, file_hash, now)
                self.logger.info(f"Successfully processed and marked: {filepath}")
                
                # Small sleep to prevent rate limits
                time.sleep(2)
                
            except Exception as e:
                self.logger.error(f"Failed to process {filepath}: {e}")
                
if __name__ == "__main__":
    daemon = IngestDaemon()
    daemon.process_pending()
