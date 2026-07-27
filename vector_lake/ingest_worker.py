import logging
import json
import threading

from vector_lake.db_store import (
    claim_pending_jobs,
    enqueue_ingest_task_cleanup,
    get_connection,
    mark_job_awaiting_subagent,
    renew_job_dispatch_lease,
    transaction,
    update_job_status,
)
from vector_lake.native_llm import create_subagent_task
from vector_lake.watchdog_status import write_status

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



def _persist_dispatch_packet_cleanup(job_id: str, task_path) -> bool:
    """Durably retire a packet that lost its dispatch lease before handoff."""
    try:
        with transaction():
            enqueue_ingest_task_cleanup(str(job_id), str(task_path))
        return True
    except Exception as exc:
        log.error("Could not persist stale packet cleanup for %s: %s", job_id, exc)
        return False

def process_jobs():
    from vector_lake.tool_ingest import (
        process_ingest_task_cleanup,
        requeue_legacy_ingest_jobs,
    )

    process_ingest_task_cleanup(limit=20)
    requeue_legacy_ingest_jobs()
    jobs = claim_pending_jobs(limit=1, lease_seconds=3600)
    if not jobs:
        return

    for job in jobs:
        job_id = str(job["job_id"])
        task_type = str(job["task_type"] or "")
        lease_owner = str(job["lease_owner"] or "")
        lease_token = str(job["lease_token"] or "")
        lease_generation = int(job["lease_generation"] or 0)
        task_path = None
        handed_off = False

        try:
            if not renew_job_dispatch_lease(
                job_id,
                lease_owner,
                lease_token,
                lease_generation,
                lease_seconds=3600,
            ):
                log.warning(
                    "Skipped stale dispatch lease for job %s before task creation",
                    job_id,
                )
                continue
            payload = json.loads(job["payload"])
            log.info("Dispatched job %s of type %s", job_id, task_type)

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
                    "ingest_contract_version": payload.get(
                        "ingest_contract_version"
                    ),
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
                handed_off = mark_job_awaiting_subagent(
                    job_id,
                    str(task_path),
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                )
                if not handed_off:
                    _persist_dispatch_packet_cleanup(job_id, task_path)
                    log.warning(
                        "Discarded stale subagent packet for job %s after lease loss",
                        job_id,
                    )
                    continue
                log.info("Created subagent ingest task for job %s: %s", job_id, task_path)
            else:
                if not update_job_status(
                    job_id,
                    "failed",
                    f"Unknown task_type {task_type}",
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                ):
                    log.warning("Ignored stale failure update for job %s", job_id)

        except Exception as exc:
            log.error("Job %s failed: %s", job_id, exc)
            if task_path is not None and not handed_off:
                _persist_dispatch_packet_cleanup(job_id, task_path)
            try:
                updated = update_job_status(
                    job_id,
                    "failed",
                    str(exc),
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                )
            except Exception as status_exc:
                log.error("Could not persist failure for job %s: %s", job_id, status_exc)
            else:
                if not updated:
                    log.warning("Ignored stale failure update for job %s", job_id)
def start_worker(stop_event: threading.Event | None = None):
    """Run the ingest dispatcher until the shared watchdog stop event is set."""
    stop_event = stop_event or threading.Event()
    log.info("Ingest Worker Daemon started.")
    write_status("idle", 0, 0, "Ingest worker started", "", component="ingest")
    try:
        while not stop_event.is_set():
            try:
                write_status(
                    "processing",
                    0,
                    0,
                    "Checking ingest dispatch queue",
                    "",
                    component="ingest",
                )
                process_jobs()
                write_status(
                    "idle",
                    0,
                    0,
                    "Ingest dispatcher heartbeat",
                    "",
                    component="ingest",
                )
                if stop_event.wait(5):
                    break
            except Exception as exc:
                log.error("Worker exception: %s", exc)
                write_status(
                    "error",
                    0,
                    0,
                    "Ingest dispatcher exception",
                    str(exc),
                    component="ingest",
                )
                if stop_event.wait(15):
                    break
    finally:
        write_status(
            "stopped",
            0,
            0,
            "Ingest worker stopped",
            "",
            component="ingest",
        )

if __name__ == "__main__":
    start_worker()
