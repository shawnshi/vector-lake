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
    jobs = get_pending_jobs(limit=2)
    if not jobs:
        return

    import subprocess
    import tempfile
    import os

    dispatched_jobs = []
    temp_files = []
    
    # Pre-process jobs
    for job in jobs:
        job_id = job["job_id"]
        task_type = job["task_type"]
        payload = json.loads(job["payload"])
        
        if task_type != "ingest":
            update_job_status(job_id, "failed", f"Unknown task_type {task_type}")
            continue
            
        update_job_status(job_id, "dispatched")
        log.info(f"Batched job {job_id} for subagent dispatch.")
        dispatched_jobs.append(job_id)
        
        # Write instructions to a temp file
        instructions = payload["instructions"]
        fd, temp_path = tempfile.mkstemp(suffix=".md", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(instructions)
        temp_files.append((job_id, temp_path))

    if not temp_files:
        return

    log.info(f"Invoking Master AgY agent to dispatch {len(temp_files)} subagents concurrently...")
    
    # Construct master prompt
    master_prompt = "You are the Vector Lake Master Dispatcher.\n"
    master_prompt += f"Your task is to use the `invoke_subagent` tool to spawn {len(temp_files)} subagents concurrently. Do NOT process them sequentially yourself.\n\n"
    
    subagents_config = []
    for i, (j_id, t_path) in enumerate(temp_files):
        prompt = f"Your assignment is to execute a Vector Lake ingestion task.\nStep 1: Read the strict instruction set at: {t_path} using view_file.\nStep 2: Read the raw source file specified in the instructions.\nStep 3: Extract the knowledge according to the Vector Lake Schema.\nStep 4: Write the extracted nodes to a temporary JSON file.\nStep 5: Call `finalize_ingest` MCP tool with the temporary JSON files.\nReply to me when you are completely finished."
        master_prompt += f"Subagent {i+1} Setup:\n- Role: Ingest Agent {i+1}\n- TypeName: self\n- Prompt: Read instructions from {t_path} and execute `finalize_ingest`.\n\n"
    
    master_prompt += "Invoke all subagents in a SINGLE tool call to `invoke_subagent`. Wait for all of them to reply with success. If any fail, note it. Finally, reply indicating the batch is complete and stop."

    master_fd, master_temp_path = tempfile.mkstemp(suffix=".md", text=True)
    with os.fdopen(master_fd, "w", encoding="utf-8") as f:
        f.write(master_prompt)

    try:
        proc = subprocess.run([
            "powershell", "-Command", 
            f'agy run --prompt (Get-Content "{master_temp_path}" -Raw) --dangerously-skip-permissions'
        ], capture_output=True, text=True, check=False, creationflags=0x08000000)
        
        if proc.returncode == 0:
            for j_id in dispatched_jobs:
                update_job_status(j_id, "finalized")
            log.info(f"Finalized {len(dispatched_jobs)} jobs via Master Agent dispatch.")
        else:
            raise Exception(f"Master Agent failed with code {proc.returncode}: {proc.stderr}\n{proc.stdout}")
    except Exception as e:
        log.error(f"Batch dispatch failed: {e}")
        for j_id in dispatched_jobs:
            update_job_status(j_id, "failed", str(e))
    finally:
        try:
            os.remove(master_temp_path)
            for _, t_path in temp_files:
                os.remove(t_path)
        except OSError:
            pass

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
