#!/usr/bin/env python
"""Host-side ingest runner (route B) - shadow mode first.

Why this lives outside ``vector_lake/``: the runtime must not make non-embedding model
calls (the task packet's ``cost_boundary``).  The runner is the host-side consumer that
claims work, (optionally) invokes a model, and submits through the one governed commit
entrypoint, ``finalize_ingest``.

What it implements from the contract in this session's design note:

  C1  skip work whose raw source is already published - no model call, and the job is
      closed with ``rejected`` + reason instead of being queued forever
  C3  bounded attempts; never re-claim a job to "try once more"
  C4  all-or-nothing payload with an explicit integration disposition
  C7  one task's failure never aborts the batch; every task is accounted for in the
      status file

Shadow mode (default) never writes a page: tasks that would need real content are left
leased and reported as ``needs-model``, so the pass rate can be measured before enabling
writes.  The model seam is ``--model-cmd`` (receives the packet JSON on stdin, must emit
``{"files_written": [...], "integration": {...}}`` on stdout); it is unused unless supplied.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vector_lake.runtime_environment import configure_numeric_threads  # noqa: E402

configure_numeric_threads()

from vector_lake import db_store  # noqa: E402
from vector_lake.process_control import run_contained, model_timeout_seconds  # noqa: E402
from vector_lake.tool_ingest import (  # noqa: E402
    claim_ingest_tasks,
    finalize_ingest,
    raw_is_published,
    raw_publication_index,
    record_ingest_failure,
)

def _runtime_dir() -> Path:
    """Resolved through the library so no install-specific path ships in the source."""
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "runtime"


STATUS_PATH = _runtime_dir() / "runner_status.json"
REJECT_DUPLICATE = (
    "该原始文件的 Source 页已发布（frontmatter sources 已声明此 raw 路径），"
    "此任务为重复准备；由 ingest runner 自动关闭以免重复入库。"
)
REJECT_MISSING_SOURCE = "原始文件在 raw 目录下已不存在，任务无法完成；由 ingest runner 自动关闭。"


def _merge_model_integration(processed: dict, integration: dict) -> dict:
    """Keep lease and source versions host-owned; accept only the model's semantic decision."""
    if not isinstance(integration, dict) or not str(integration.get("disposition") or "").strip():
        raise ValueError("model output requires an explicit integration disposition")
    return {**processed, "integration": integration}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Bounded repair rounds after a finalize rejection.  The model seam is a language model, so a
#: rejection is usually a wording or shape slip rather than a missing fact: handing the validator's
#: own message back costs one extra model call and converts a lost attempt (and, at the cap, an
#: abandoned source) into a corrected answer.  Measured 2026-09-28, a single paper source stacked
#: several independent schema violations (a naming violation, then a missing tension slot), which
#: is why the budget is a count and not a flag: one round fixes one fault, and the source still
#: converges through its remaining attempt budget.  Bounded on purpose -- a source that fails the
#: same check on every round has a real problem the budget must not paper over.
REPAIR_ATTEMPTS = max(0, int(os.environ.get("VECTOR_LAKE_RUNNER_REPAIR_ATTEMPTS", "2")))


def _repair_packet(packet: dict, previous_output: dict, validation_error: str) -> dict:
    """Add the rejection to a copy of the packet; the seam turns it into a repair brief."""
    metadata = dict(packet.get("metadata") or {})
    metadata["repair"] = {
        "previous_output": previous_output,
        "validation_error": str(validation_error)[:2000],
    }
    return {**packet, "metadata": metadata}


def write_status(**fields) -> None:
    """Write a clean snapshot.

    Every caller passes the complete ``stats`` mapping, so merging with the previous
    file only leaked stale keys (a leftover ``reason: "once"`` was reported while the
    resident runner was mid-cycle).
    """
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(fields)
    payload["updated_at"] = _utc_now()
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def classify(processed: dict, index: dict) -> str:
    """C1: decide from evidence before spending anything on the task."""
    filepath = str(processed.get("filepath") or "")
    if not filepath or not Path(filepath).exists():
        return "missing-source"
    if raw_is_published(filepath, index):
        return "duplicate"
    return "needs-model"


def _version_conflict(message: str) -> bool:
    # Preserve optimistic locking. A new dispatch refreshes candidates; never patch tokens
    # into an old model proposal. Missing tokens are still ordinary model-repair errors.
    return any(marker in message for marker in (
        "target_hash is stale", "target_projection_hash is stale", "source_hash is stale",
        "source_projection_hash is stale", "source changed", "lease is stale",
        "Canonical version conflict",
    ))


def run_model(packet: dict, model_cmd: str) -> tuple:
    """Invoke the pluggable model seam; return its bounded semantic result or an error."""
    timeout = model_timeout_seconds()
    deadline = float(packet.get("_host_deadline_monotonic", time.monotonic() + timeout))
    remaining = min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        return None, "model execution deadline exhausted"
    env = dict(os.environ)
    env["VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC"] = str(deadline)
    proc = run_contained(
        model_cmd, shell=True, input=json.dumps(packet, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=remaining, env=env,
    )
    if proc.returncode != 0:
        return None, f"model runner exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    try:
        result = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        return None, f"model runner JSON invalid: {exc}"
    if not isinstance(result, dict) or set(result) != {"files_written", "integration"}:
        return None, "model runner must return files_written and integration only"
    files = result["files_written"]
    integration = result["integration"]
    if not isinstance(files, list) or not all(
        isinstance(item, dict) and isinstance(item.get("filename"), str)
        and isinstance(item.get("content"), str) for item in files
    ):
        return None, "model runner files_written must contain filename/content objects"
    if not isinstance(integration, dict) or not str(integration.get("disposition") or "").strip():
        return None, "model runner requires an explicit integration disposition"
    if not files and str(integration["disposition"]).strip().lower() != "rejected":
        return None, "model runner produced no files for a non-rejected disposition"
    return result, ""


def _process_task(task: dict, shadow: bool, model_cmd: str, publication_index: dict) -> tuple[dict, str]:
    """Process a single claimed ingest task in isolation (C7 error containment)."""
    res = {"duplicate": 0, "missing-source": 0, "needs-model": 0, "finalized": 0, "errors": 0, "model-failed": 0}
    last_err = ""
    packet = dict(task.get("task_packet") or {})
    # One host deadline includes all repair rounds; retain a lease/finalization margin.
    budget = model_timeout_seconds()
    lease_until = task.get("lease_until")
    if lease_until:
        expires = datetime.fromisoformat(str(lease_until).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        budget = min(budget, expires.timestamp() - datetime.now(timezone.utc).timestamp() - 120.0)
        if budget <= 0:
            # Do not mutate a potentially expired/newly-owned claim or spend a source attempt.
            res["needs-model"] += 1
            return res, "claim has insufficient execution window; awaiting lease recovery"
    packet["_host_deadline_monotonic"] = time.monotonic() + budget
    processed = (packet.get("metadata") or {}).get("processed_data")
    job_id = str(task.get("job_id") or "")
    if packet.get("error") or not processed:
        reason = str(packet.get("error") or "task packet carries no processed_data")
        res["errors"] += 1
        last_err = reason
        if job_id:
            record_ingest_failure(job_id, f"unusable task packet: {reason}")
        return res, last_err

    verdict = classify(processed, publication_index)
    try:
        if verdict in {"duplicate", "missing-source"}:
            reason = REJECT_DUPLICATE if verdict == "duplicate" else REJECT_MISSING_SOURCE
            result = finalize_ingest([], {**processed, "integration": {"disposition": "rejected", "reason": reason}})
            res[verdict] += 1
            if "Successfully finalized" in result:
                res["finalized"] += 1
            else:
                res["errors"] += 1
                last_err = result
                if job_id:
                    record_ingest_failure(job_id, f"rejection could not be finalized: {result}")
        elif shadow or not model_cmd:
            res["needs-model"] += 1
            if job_id and os.environ.get("VECTOR_LAKE_RUNNER_HOLD_SHADOW_LEASE", "0") != "1":
                from vector_lake.db_store import release_job_for_retry
                release_job_for_retry(job_id, "shadow run: model call skipped")
        else:
            model_result, error = run_model(packet, model_cmd)
            if error:
                res["model-failed"] += 1
                last_err = error
                if job_id:
                    record_ingest_failure(job_id, f"model seam: {error}")
                return res, last_err
            result = finalize_ingest(
                model_result["files_written"],
                _merge_model_integration(processed, model_result["integration"]),
            )
            # Rejection is information, not a verdict: hand the model its own validator message
            # back, up to the bounded budget, before spending the job's attempt.
            rejection = "" if "Successfully finalized" in result else result
            previous_output = model_result
            rounds_used = 0
            while rejection and not _version_conflict(rejection) and rounds_used < REPAIR_ATTEMPTS:
                rounds_used += 1
                repaired, repair_error = run_model(
                    _repair_packet(packet, previous_output, rejection), model_cmd
                )
                if repair_error or not repaired:
                    result = (
                        f"{rejection} [repair round {rounds_used} could not produce an answer: "
                        f"{repair_error or 'empty model output'}]"
                    )
                    break
                previous_output = repaired
                result = finalize_ingest(
                    repaired["files_written"],
                    _merge_model_integration(processed, repaired["integration"]),
                )
                rejection = "" if "Successfully finalized" in result else result
            if rounds_used and rejection:
                result = f"{result} [after {rounds_used} repair round(s)]"
            if "Successfully finalized" in result:
                res["finalized"] += 1
            else:
                res["errors"] += 1
                last_err = result
                if job_id:
                    record_ingest_failure(job_id, f"finalize rejected: {result}")
    except Exception as exc:
        res["errors"] += 1
        last_err = f"{type(exc).__name__}: {exc}"
        if job_id:
            record_ingest_failure(job_id, f"{type(exc).__name__}: {exc}")
    return res, last_err


def process_once(limit: int, shadow: bool, model_cmd: str, stats: dict, concurrency: int = 1) -> dict:
    claimed = json.loads(claim_ingest_tasks(limit=limit))
    batch = {"claimed": len(claimed), "duplicate": 0, "missing-source": 0,
             "needs-model": 0, "finalized": 0, "errors": 0, "model-failed": 0}
    if not claimed:
        return batch
    publication_index = raw_publication_index()

    if concurrency > 1 and len(claimed) > 1:
        workers = min(concurrency, len(claimed))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_process_task, task, shadow, model_cmd, publication_index)
                for task in claimed
            ]
            for f in futures:
                task_res, err = f.result()
                for k, v in task_res.items():
                    batch[k] += v
                if err:
                    stats["last_error"] = err
    else:
        for task in claimed:
            task_res, err = _process_task(task, shadow, model_cmd, publication_index)
            for k, v in task_res.items():
                batch[k] += v
            if err:
                stats["last_error"] = err

    return batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Vector Lake ingest runner (shadow first)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--once", action="store_true", help="run a single batch and exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between batches in loop mode")
    parser.add_argument("--concurrency", "-c", type=int,
                        default=int(os.environ.get("VECTOR_LAKE_RUNNER_CONCURRENCY", "1")),
                        help="concurrent worker threads for model execution (default 1)")
    parser.add_argument("--shadow", action="store_true", default=True)
    parser.add_argument("--no-shadow", dest="shadow", action="store_false",
                        help="allow real writes (requires --model-cmd)")
    parser.add_argument("--model-cmd", default=os.environ.get("VECTOR_LAKE_RUNNER_MODEL_CMD", ""))
    args = parser.parse_args()

    pid_path = _runtime_dir() / "runner.pid"
    stats = {"started_at": _utc_now(), "pid": os.getpid(), "shadow": args.shadow,
             "model_command": bool(args.model_cmd), "interval": args.interval,
             "last_error": "", "consecutive_failures": 0, "cycles": 0, "totals": {}}
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    def _shutdown(signum, _frame):
        write_status(status="stopped", reason=f"signal {signum}", **stats)
        try:
            pid_path.unlink()
        except OSError:
            pass
        raise SystemExit(0)

    for _sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, _sig):
            try:
                signal.signal(getattr(signal, _sig), _shutdown)
            except (ValueError, OSError):
                pass
    write_status(status="running", **stats)

    while True:
        try:
            batch = process_once(args.limit, args.shadow, args.model_cmd, stats, args.concurrency)
            totals = stats["totals"]
            for key, value in batch.items():
                totals[key] = int(totals.get(key, 0)) + int(value)
            # In shadow mode "needs-model" is the expected outcome, not a failure:
            # counting it as no-progress made every shadow pass report runner_failing.
            stats["cycles"] = int(stats.get("cycles", 0)) + 1
            progressed = (batch["finalized"] + batch["needs-model"]) > 0
            stats["consecutive_failures"] = 0 if progressed or batch["claimed"] == 0 else stats["consecutive_failures"] + 1
            write_status(status="idle" if batch["claimed"] == 0 else "processing",
                         last_batch=batch, last_batch_at=_utc_now(), **stats)
            if args.once:
                write_status(status="stopped", reason="once", **stats)
                try:
                    pid_path.unlink()
                except OSError:
                    pass
                return 0
            if stats["consecutive_failures"] >= 5:
                # Resident process: cool down and keep the heartbeat alive instead of
                # exiting, so a transient outage does not silently end supervision.
                cooldown = max(60, int(os.environ.get("VECTOR_LAKE_RUNNER_COOLDOWN", "300")))
                # ``recovering``: a cooldown that retries is not a terminal state (S4).
                write_status(status="recovering", reason=f"consecutive failures; cooling down {cooldown}s", **stats)
                time.sleep(cooldown)
                stats["consecutive_failures"] = 0
            if batch["claimed"] == 0 and args.once:
                return 0
        except Exception as exc:  # never die silently
            stats["consecutive_failures"] += 1
            stats["last_error"] = f"{type(exc).__name__}: {exc}"
            write_status(status="error", **stats)
            if args.once:
                return 1
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
