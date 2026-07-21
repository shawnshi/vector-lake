import time
import logging
import json

from vector_lake.db_store import claim_pending_jobs, get_connection, mark_job_awaiting_subagent, update_job_status
from vector_lake.native_llm import create_subagent_task

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingest-worker")


def _ingest_finalization_proven(filepath: str, file_hash: str) -> bool:
    row = get_connection().execute(
        "SELECT file_hash FROM processed_files WHERE filepath = ?",
        (filepath,),
    ).fetchone()
    return bool(row and row["file_hash"] == file_hash)


def _subagent_ingest_prompt(instructions: str) -> str:
    return (
        instructions
        + "\n\n[CURRENT-ENVIRONMENT SUBAGENT HANDOFF]\n"
        + "You are the host environment subagent completing this Vector Lake ingest task.\n"
        + "Do not use external model APIs from Vector Lake library code.\n"
        + "Return or persist ONLY a JSON array. Each item must be an object with exactly these keys:\n"
        + "- filename: target wiki filename\n"
        + "- content: complete Markdown content, including YAML frontmatter\n"
        + "Add processed_data.integration with disposition integrated, standalone, or rejected.\n"
        + "Preserve the task packet source_hash. Integrated relations must use candidate canonical target_hash values; standalone and rejected require an auditable reason.\n"
        + "After producing both payloads, call the Vector Lake finalize_ingest tool or CLI-compatible finalize path with the processed_data object from this task packet.\n"
    )


def process_jobs():
    from vector_lake.tool_ingest import requeue_legacy_ingest_jobs

    requeue_legacy_ingest_jobs()
    jobs = claim_pending_jobs(limit=1, lease_seconds=3600)
    if not jobs:
        return


    # Pre-process jobs
    for job in jobs:
        job_id = job["job_id"]
        task_type = job["task_type"]
        payload = json.loads(job["payload"])
        
        try:
            log.info(f"Dispatched job {job_id} of type {task_type}")
            
            if task_type == "ingest":
                filepath = payload["filepath"]
                file_hash = payload["hash"]
                instructions = payload["instructions"]
                canonical_name = payload["canonical_name"]

                processed_data = {
                    "filepath": filepath,
                    "hash": file_hash,
                    "canonical_name": canonical_name,
                    "source_hash": str(payload.get("source_hash") or ""),
                    "ingest_contract_version": payload.get("ingest_contract_version"),
                    "job_id": job_id,
                }
                task_path = create_subagent_task(
                    "ingest",
                    _subagent_ingest_prompt(instructions),
                    "JSON array consumable by finalize_ingest(files_written, processed_data)",
                    {
                        "job_id": job_id,
                        "processed_data": processed_data,
                        "finalize_tool": "finalize_ingest",
                    },
                )
                mark_job_awaiting_subagent(job_id, str(task_path))
                log.info(f"Created subagent ingest task for job {job_id}: {task_path}")
                
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
