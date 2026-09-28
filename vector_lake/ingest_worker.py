import time
import logging
import json

from vector_lake.db_store import claim_pending_jobs, get_connection, mark_job_awaiting_subagent, update_job_status
from vector_lake.native_llm import create_subagent_task
from vector_lake.output_contract import build_output_contract
from vector_lake.schema_validator import INGEST_INTEGRATION_PREDICATES, VALID_PREDICATES

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
        + "Return ONLY one JSON object with exactly two keys:\n"
        + "- files_written: array of filename/content objects (complete Markdown with YAML frontmatter)\n"
        + "- integration: explicit disposition with auditable reason or validated relations\n"
        + "Frontmatter rules the validator enforces (violating one refuses the whole ingest):\n"
        + "- categories: a YAML list with EXACTLY one element, one of the SCHEMA_CATEGORIES domains"
        + ' (e.g. categories: ["Healthcare_IT"]); never a bare string, never two elements\n'
        + "- strategic_scope: exactly `core` or `edge`; aliases: a list; epistemic-status is one of"
        + " sprouting/evergreen/seed; and id/title/type/domain/status/updated/sources present"
        + " -- the field list the schema gate itself requires. `topic_cluster` is optional (the"
        + " gate defaults it to `General`), and `ttl`, `memory_type` and `memory_key` are not"
        + " fields the gate requires of a wiki page\n"
        + "- tags: at most 3, and none may equal an existing node's `title` or any of its `aliases`"
        + " (compared lowercased). The gate calls that `Tag Collision` and refuses the whole ingest,"
        + " so do not tag a term that already has a page -- including under an alias rather than"
        + " the page's own title: `电子病历评级` and `EMR评级` are both refused because"
        + " `Policy_电子病历系统功能应用水平分级评价` declares them as aliases. Express the relation"
        + " as a typed link instead, e.g. `[related_to:: [[Standard_电子病历评级]]]` in Section 1.\n"
        + "- every typed link must be [predicate:: [[Target]]] with a predicate from this closed"
        + f" vocabulary: {', '.join(sorted(VALID_PREDICATES))}\n"
        + "- an integration relation's `predicate` is NOT drawn from that list: it binds a document"
        + " to an existing page, so only this narrower set is accepted --"
        + f" {', '.join(sorted(INGEST_INTEGRATION_PREDICATES))}. `has_part` and the other structural"
        + " predicates are refused here\n"
        + "- every relation `target` must be copied verbatim from the packet's integration_candidates"
        + " manifest, `.md` suffix included; a rephrased or suffix-less name is refused\n"
        + "- a bullet under `## 2. 证据时间线` must be `- [YYYY-MM-DD] [Event_Tag] <event>` with a tag"
        + " from exactly: [Release], [Pivot], [Conflict], [Validation], [Observation], [Decision],"
        + " [Execution], [Outcome]; an undated item (an open question, a 未决点) does not belong in"
        + " that list at all, and no date may be invented to satisfy the shape\n"
        + "Do not echo or alter processed_data; the host retains the task packet's lease and source_hash.\n"
        + "Integrated relations must use candidate canonical target_hash values; standalone and rejected require an auditable reason.\n"
        + "Each integrated relation is a complete checked record: target (verbatim from the manifest,\n"
        + "`.md` included), target_hash + target_projection_hash (copied from that entry, not computed),\n"
        + "predicate (from the narrower set above), evidence (>= 12 chars), confidence (a number in\n"
        + "[0,1]), event_date (YYYY-MM-DD), event_tag (one bare tag, brackets omitted, from Release,\n"
        + "Pivot, Conflict, Validation, Observation, Decision, Execution, Outcome). One bad field\n"
        + "refuses the whole ingest.\n"
        + "Do not call finalize_ingest: the host validates and submits the returned object.\n"
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
                    "JSON object with files_written and integration; host calls finalize_ingest",
                    {
                        "job_id": job_id,
                        "processed_data": processed_data,
                        "finalize_tool": "finalize_ingest",
                        # The machine-checked output shape travels with the packet and the model
                        # seam appends it to the end of the brief.  Buried in the ~50 KB prompt
                        # it was obeyed by luck; beside the required output it is a checklist.
                        "output_contract": build_output_contract(),
                    },
                )
                mark_job_awaiting_subagent(job_id, str(task_path))
                log.info(f"Created subagent ingest task for job {job_id}: {task_path}")
                
            else:
                update_job_status(job_id, "failed", f"Unknown task_type {task_type}")
                
        except Exception as e:
            log.error(f"Job {job_id} failed: {e}")
            update_job_status(job_id, "failed", str(e))

#: Normal cadence of the ingest worker loop.
INGEST_WORKER_INTERVAL_SECONDS = 5.0

#: Cadence while another writer is contending for the write lock.
#:
#: This loop takes the write lock on every tick, so it is the one that should yield: it is a
#: periodic housekeeper, while whatever it is contending with is usually a bounded one-off (a
#: governance batch, a backfill, an operator script).  Measured 2026-09-28: a batch of 19 merges
#: waited up to 346 s per write and took ~10 minutes in total, because this loop re-took the lock
#: every 5 seconds.  Yielding costs nothing when nobody else is writing -- the marker this reads is
#: only written when a lock acquisition actually had to retry.
INGEST_WORKER_CONTENDED_INTERVAL_SECONDS = 30.0

#: How long a recorded contention keeps the worker in the longer cadence.  Long enough to cover a
#: multi-minute batch, short enough that it returns to normal on its own if the marker goes stale.
INGEST_WORKER_CONTENTION_MEMORY_SECONDS = 300.0


def _recent_write_contention() -> bool:
    """Is another writer contending for the write lock right now?

    Reads the marker ``db_store._record_lock_contention`` writes.  A lock that was taken on the
    first attempt records nothing, so a worker operating alone never sees contention and never
    slows down; the marker only exists while two writers are actually overlapping.
    """
    import json
    from datetime import datetime, timezone

    from vector_lake.wiki_utils import get_meta_dir

    marker = get_meta_dir() / "runtime" / "write_lock_contention.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    # The real marker is ``{"last": {...}, "history": [...]}``; the newest entry is ``last``.
    # Reading a top-level ``at`` (as the first cut did, and as its test wrongly assumed) always
    # yields None and therefore never yields the loop.
    last = payload.get("last")
    if not isinstance(last, dict):
        return False
    try:
        stamp = datetime.fromisoformat(str(last.get("at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    return 0 <= age <= INGEST_WORKER_CONTENTION_MEMORY_SECONDS


def _idle_delay() -> float:
    """How long to wait before the next tick: the normal cadence, or the yielded one."""
    if _recent_write_contention():
        return INGEST_WORKER_CONTENDED_INTERVAL_SECONDS
    return INGEST_WORKER_INTERVAL_SECONDS


def start_worker():
    log.info("Ingest Worker Daemon started.")
    while True:
        try:
            process_jobs()
            delay = _idle_delay()
            if delay > INGEST_WORKER_INTERVAL_SECONDS:
                log.info(
                    "Ingest worker yielding to another writer; next tick in %.0fs", delay
                )
            time.sleep(delay)
        except Exception as e:
            log.error(f"Worker exception: {e}")
            time.sleep(15)

if __name__ == "__main__":
    start_worker()
