"""Current-environment subagent handoff for text-generation work.

Vector Lake may still use model APIs for embeddings in the indexer/search
pipeline. Non-embedding text generation is intentionally not performed from
library code because that silently creates external model cost. When a runtime
path needs reasoning, it writes a bounded task packet that the host agent or a
current-environment subagent can execute explicitly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("vector-lake-native-llm")
_DEFAULT_RUN_ID = f"runtime-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class NativeLLMUnavailable(RuntimeError):
    """Raised when in-process text generation is intentionally unavailable."""


class SubagentTaskRequired(NativeLLMUnavailable):
    """Raised after a current-environment subagent task packet is created."""

    def __init__(self, task_path: Path, detail: str):
        self.task_path = task_path
        super().__init__(f"{detail}: {task_path}")


def native_llm_ready() -> tuple[bool, str]:
    return False, "text generation is delegated to current-environment subagent task packets"


def get_subagent_scratch_dir() -> Path:
    """Return the isolated scratch directory for the active host-agent run."""
    from vector_lake import get_extension_root

    run_id = os.environ.get("VECTOR_LAKE_SUBAGENT_RUN_ID", _DEFAULT_RUN_ID)
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id).strip(".-") or "subagent-runtime"
    root = get_extension_root() / "brain" / safe_run_id / "scratch"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _task_root() -> Path:
    root = get_subagent_scratch_dir() / "subagent_tasks"
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_subagent_task(
    task_type: str,
    prompt: str,
    expected_output: str,
    metadata: dict[str, Any] | None = None,
) -> Path:
    task_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    task_path = _task_root() / f"{task_id}.json"
    payload = {
        "task_id": task_id,
        "task_type": task_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": "current-environment-subagent",
        "cost_boundary": "no non-embedding model API calls from Vector Lake runtime",
        "expected_output": expected_output,
        "metadata": metadata or {},
        "prompt": prompt,
    }
    tmp_path = task_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, task_path)
    return task_path


def remove_subagent_task(
    task_path: str | Path,
    *,
    expected_job_id: str | None = None,
    expected_task_type: str | None = None,
    expected_task_id: str | None = None,
) -> bool:
    """Delete one verified packet from an exact per-run subagent task directory."""
    from vector_lake import get_extension_root

    candidate = Path(task_path).resolve()
    brain_root = (get_extension_root() / "brain").resolve()
    try:
        relative = candidate.relative_to(brain_root)
    except ValueError as exc:
        raise ValueError(
            f"Refusing to remove task packet outside isolated brain tree: {candidate}"
        ) from exc
    if (
        candidate.suffix.lower() != ".json"
        or len(relative.parts) != 4
        or relative.parts[1:3] != ("scratch", "subagent_tasks")
    ):
        raise ValueError(f"Refusing to remove task packet outside isolated brain tree: {candidate}")
    if expected_task_id and candidate.stem != str(expected_task_id):
        raise ValueError(
            f"Task packet filename does not match expected task id: {candidate}"
        )
    if not candidate.exists():
        return False
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Task packet is unreadable: {candidate}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Task packet payload is not an object: {candidate}")
    packet_task_id = str(payload.get("task_id") or "")
    if not packet_task_id or packet_task_id != candidate.stem:
        raise ValueError(f"Task packet identity does not match its filename: {candidate}")
    if expected_task_id and packet_task_id != str(expected_task_id):
        raise ValueError(f"Task packet id does not match cleanup intent: {candidate}")
    if expected_task_type and str(payload.get("task_type") or "") != str(expected_task_type):
        raise ValueError(f"Task packet type does not match cleanup intent: {candidate}")
    if expected_job_id:
        metadata = payload.get("metadata")
        packet_job_id = (
            str(metadata.get("job_id") or "")
            if isinstance(metadata, dict)
            else ""
        )
        if packet_job_id != str(expected_job_id):
            raise ValueError(f"Task packet job id does not match cleanup intent: {candidate}")
    candidate.unlink()
    return True


def generate_text(prompt: str, model: str | None = None) -> str:
    task_path = create_subagent_task(
        "text_generation",
        prompt,
        "plain text",
        {"model_ignored": model, "legacy_entrypoint": "generate_text"},
    )
    raise SubagentTaskRequired(task_path, "text generation requires host subagent execution")


async def async_generate_text(prompt: str, model: str | None = None) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, generate_text, prompt, model)


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\}|\[.*\])", stripped, re.DOTALL)
    if not match:
        raise ValueError("No JSON object or array found in native LLM response.")
    return json.loads(match.group(1))


def generate_json_array(prompt: str, model: str | None = None) -> list[Any]:
    task_path = create_subagent_task(
        "json_array_generation",
        prompt,
        "JSON array",
        {"model_ignored": model, "legacy_entrypoint": "generate_json_array"},
    )
    raise SubagentTaskRequired(task_path, "JSON array generation requires host subagent execution")


def generate_json_object(prompt: str, model: str | None = None) -> dict[str, Any]:
    task_path = create_subagent_task(
        "json_object_generation",
        prompt,
        "JSON object",
        {"model_ignored": model, "legacy_entrypoint": "generate_json_object"},
    )
    raise SubagentTaskRequired(task_path, "JSON object generation requires host subagent execution")
