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
the contract JSON array on stdout); it is unused unless supplied.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vector_lake import db_store  # noqa: E402
from vector_lake.tool_ingest import (  # noqa: E402
    claim_ingest_tasks,
    finalize_ingest,
    raw_is_published,
    raw_publication_index,
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def run_model(packet: dict, model_cmd: str) -> tuple:
    """Invoke the pluggable model seam; returns (files_written, error)."""
    proc = subprocess.run(
        model_cmd, shell=True, input=json.dumps(packet, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=int(os.environ.get("VECTOR_LAKE_RUNNER_MODEL_TIMEOUT", "900")),
    )
    if proc.returncode != 0:
        return None, f"model runner exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    text = proc.stdout
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None, f"model runner produced no JSON array: {text[:200]}"
    try:
        files = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        return None, f"model runner JSON invalid: {exc}"
    if not isinstance(files, list) or not files:
        return None, "model runner produced an empty payload"
    return files, ""


def process_once(limit: int, shadow: bool, model_cmd: str, stats: dict) -> dict:
    claimed = json.loads(claim_ingest_tasks(limit=limit))
    batch = {"claimed": len(claimed), "duplicate": 0, "missing-source": 0,
             "needs-model": 0, "finalized": 0, "errors": 0, "model-failed": 0}
    if not claimed:
        return batch
    publication_index = raw_publication_index()

    for task in claimed:
        packet = task.get("task_packet") or {}
        processed = (packet.get("metadata") or {}).get("processed_data")
        if packet.get("error") or not processed:
            # C7: leave it to the runtime (it retires unreadable packets at claim time).
            batch["errors"] += 1
            continue
        verdict = classify(processed, publication_index)
        try:
            if verdict in {"duplicate", "missing-source"}:
                reason = REJECT_DUPLICATE if verdict == "duplicate" else REJECT_MISSING_SOURCE
                result = finalize_ingest([], {**processed, "integration": {"disposition": "rejected", "reason": reason}})
                batch[verdict] += 1
                if "Successfully finalized" in result:
                    batch["finalized"] += 1
                else:
                    # A rejected finalize used to leave every counter at zero, so a
                    # blocking gate looked like "no progress" with no recorded reason.
                    batch["errors"] += 1
                    stats["last_error"] = result
            elif shadow or not model_cmd:
                batch["needs-model"] += 1  # stays leased; nothing written
            else:
                files, error = run_model(packet, model_cmd)
                if error:
                    batch["model-failed"] += 1
                    # assign, not setdefault: stats already carries "last_error": "",
                    # so setdefault silently discarded the only diagnostic for the failure.
                    stats["last_error"] = error
                    continue
                result = finalize_ingest(files, {**processed, "integration": {"disposition": "standalone",
                                                                            "reason": "ingest runner standalone ingest"}})
                if "Successfully finalized" in result:
                    batch["finalized"] += 1
                else:
                    batch["errors"] += 1
                    stats["last_error"] = result
        except Exception as exc:  # C7: contain per-task failure
            batch["errors"] += 1
            stats["last_error"] = f"{type(exc).__name__}: {exc}"
    return batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Vector Lake ingest runner (shadow first)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--once", action="store_true", help="run a single batch and exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between batches in loop mode")
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
            batch = process_once(args.limit, args.shadow, args.model_cmd, stats)
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
                write_status(status="halted", reason=f"consecutive failures; cooling down {cooldown}s", **stats)
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
