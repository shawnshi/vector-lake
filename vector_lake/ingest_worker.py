import time
import logging
import json

from vector_lake.db_store import claim_pending_jobs, get_connection, mark_job_awaiting_subagent, update_job_status
from vector_lake.native_llm import create_subagent_task
from vector_lake.schema_validator import VALID_PREDICATES

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
        + "Frontmatter rules the validator enforces (violating one refuses the whole ingest):\n"
        + "- categories: a YAML list with EXACTLY one element, one of the SCHEMA_CATEGORIES domains"
        + ' (e.g. categories: ["Healthcare_IT"]); never a bare string, never two elements\n'
        + "- strategic_scope: exactly `core` or `edge`; aliases: a list; epistemic-status is one of"
        + " sprouting/evergreen/seed; and id/title/type/domain/topic_cluster/status/ttl/memory_type/"
        + "memory_key/tags/evidence_tier present\n"
        + "- tags: at most 3, and none may equal an existing node's `title` or any of its `aliases`"
        + " (compared lowercased). The gate calls that `Tag Collision` and refuses the whole ingest,"
        + " so do not tag a term that already has a page -- including under an alias rather than"
        + " the page's own title: `电子病历评级` and `EMR评级` are both refused because"
        + " `Policy_电子病历系统功能应用水平分级评价` declares them as aliases. Express the relation"
        + " as a typed link instead, e.g. `[related_to:: [[Standard_电子病历评级]]]` in Section 1.\n"
        + "- every typed link must be [predicate:: [[Target]]] with a predicate from this closed"
        + f" vocabulary: {', '.join(sorted(VALID_PREDICATES))}\n"
        + "Add processed_data.integration with disposition integrated, standalone, or rejected.\n"
        + "Preserve the task packet source_hash. Integrated relations must use candidate canonical target_hash values; standalone and rejected require an auditable reason.\n"
        + "After producing both payloads, call the Vector Lake finalize_ingest tool or CLI-compatible finalize path with the processed_data object from this task packet.\n"
    )


def process_jobs():
    from vector_lake.tool_ingest import requeue_legacy_ingest_jobs

    requeue_legacy_ingest_jobs()
    # Short lease: this step only builds a task packet and marks the job awaiting, which is
    # milliseconds of local work.  It used to claim for an hour, so a restart (or a crash) between
    # the claim and the mark hid the job for the rest of that hour -- observed 2026-09-19: one job
    # sat ``dispatched`` for 45 minutes after a daemon restart it could not have been part of.  The
    # long lease belongs where the slow work is, i.e. the runner's claim on ``awaiting_subagent``.
    jobs = claim_pending_jobs(limit=1, lease_seconds=120)
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
                    "job_id": job_id,
                    # The dispatch manifest travels with the packet.  The prompt tells the model
                    # to preserve these fields verbatim, so the finalizer can validate relations
                    # against the candidate list the model was actually shown instead of trusting
                    # whatever it chose to name.
                    "ingest_contract_version": payload.get("ingest_contract_version"),
                    "source_projection_hash": str(payload.get("source_projection_hash") or ""),
                    # Absent stays absent: the finalizer treats "no manifest" (a pre-manifest
                    # packet, tolerated during the migration) differently from "an empty
                    # manifest" (a packet that permits no relation at all).
                    "integration_candidates": payload.get("integration_candidates"),
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
