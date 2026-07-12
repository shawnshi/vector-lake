import time
import logging
import json
import os
from pathlib import Path

from vector_lake.db_store import get_pending_jobs, update_job_status
from vector_lake import get_extension_root
from vector_lake.tool_ingest import finalize_ingest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingest-worker")

def process_jobs():
    jobs = get_pending_jobs(limit=10)
    if not jobs:
        return

    for job in jobs:
        job_id = job["job_id"]
        task_type = job["task_type"]
        payload = json.loads(job["payload"])
        
        try:
            update_job_status(job_id, "dispatched")
            log.info(f"Dispatched job {job_id} of type {task_type}")
            
            if task_type == "ingest":
                filepath = payload["filepath"]
                file_hash = payload["hash"]
                instructions = payload["instructions"]
                canonical_name = payload["canonical_name"]
                
                import subprocess
                import tempfile
                import os
                
                log.info(f"Invoking AgY agent for job {job_id} on {filepath}...")
                # Write instructions to a temp file to avoid escaping issues in CLI
                with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md", encoding="utf-8") as f:
                    f.write(instructions)
                    temp_path = f.name
                
                try:
                    # Run the agent in print mode with auto-approvals
                    proc = subprocess.run([
                        "powershell", "-Command", 
                        f'agy run --prompt (Get-Content "{temp_path}" -Raw) --dangerously-skip-permissions'
                    ], capture_output=True, text=True, check=False)
                    
                    if proc.returncode == 0:
                        update_job_status(job_id, "finalized")
                        log.info(f"Finalized job {job_id} via agent.")
                        # log.debug(f"Agent output: {proc.stdout}")
                    else:
                        raise Exception(f"Agent failed with code {proc.returncode}: {proc.stderr}\n{proc.stdout}")
                finally:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                
            else:
                update_job_status(job_id, "failed", f"Unknown task_type {task_type}")
                
        except Exception as e:
            log.error(f"Job {job_id} failed: {e}")
            update_job_status(job_id, "failed", str(e))

def start_worker():
    log.info("Ingest Worker Daemon started.")
    while True:
        try:
            process_jobs()
            time.sleep(5)
        except Exception as e:
            log.error(f"Worker exception: {e}")
            time.sleep(15)

if __name__ == "__main__":
    start_worker()
