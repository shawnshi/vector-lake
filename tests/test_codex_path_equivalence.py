"""Byte-stable characterization harness for the controller's Codex path."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

import pytest

from vector_lake import auto_ingest_worker, db_store, wiki_utils
from vector_lake.raw_revision import stable_raw_revision


_CONFIG = {
    "schema_version": 1,
    "enabled": True,
    "allow_model_processing_raw_text": True,
    "runner": "codex_exec",
    "codex_executable": "C:/codex.exe",
    "runner_codex_home": "C:/vector-lake-auto-ingest",
    "required_codex_version": "0.148.0",
    "required_codex_sha256": "a" * 64,
    "required_system_skills_sha256": "c" * 64,
    "required_models_cache_sha256": "d" * 64,
    "required_auth_identity_sha256": "b" * 64,
    "model": "gpt-5.6-terra",
    "reasoning_effort": "medium",
    "poll_seconds": 5.0,
    "timeout_seconds": 1200,
    "lease_seconds": 1320,
    "lease_renew_seconds": 120,
    "max_input_bytes": 524288,
    "max_output_bytes": 1048576,
    "max_files": 8,
    "max_attempts_per_revision": 3,
    "max_tasks_per_hour": 6,
    "max_tasks_per_24h": 20,
    "max_tokens_per_task": 32768,
    "max_reserved_tokens_per_hour": 131072,
    "max_reserved_tokens_per_24h": 655360,
    "max_consecutive_infra_failures": 3,
    "circuit_breaker_seconds": 3600,
    "max_scratch_runs": 100,
    "scratch_retention_days": 14,
    "retain_artifacts": False,
    "min_decision_confidence": 0.85,
    "auto_finalize_rejected": True,
}

_CODEX_CONFIG_FIELDS = (
    "runner",
    "codex_executable",
    "runner_codex_home",
    "required_codex_version",
    "required_codex_sha256",
    "required_system_skills_sha256",
    "required_models_cache_sha256",
    "required_auth_identity_sha256",
    "model",
    "reasoning_effort",
)
_VOLATILE_TIME_KEYS = {"started_at", "ended_at", "duration_ms", "updated_at", "at", "last_success_at"}
# error_fingerprint is a digest over error text that itself embeds absolute paths
# and ids, so it can never be byte-stable across runs. error_type and error_code
# remain verbatim, and those are the stable discriminators of a changed failure.
_VOLATILE_DIGEST_KEYS = {"error_fingerprint"}
_VOLATILE_ID_KEYS = {"attempt_id", "job_id", "lease_owner", "lease_token", "revision"}
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:tmp|private|var|home)/)")
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_RANDOM_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.IGNORECASE)


def _write_config(memory_dir: Path) -> None:
    meta = memory_dir / "wiki" / ".meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "auto_ingest_config.json").write_text(
        json.dumps(_CONFIG), encoding="utf-8"
    )


def _write_purpose_contract(memory_dir: Path) -> None:
    """The controller reads purpose.md during input validation.

    Without it the run stops at stage=input_validation with
    PurposeContractError and never reaches the generation boundary, so the
    transcript would capture a failure instead of the path under test. The
    contract text is copied from the repository's existing benchmark fixture.
    """
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.1"
intent_keywords: [test]
intent_weight_boost: 0.1
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
  derived: Derived operational evidence
sir_registry:
  - id: SIR_BENCHMARK
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Isolated equivalence-harness purpose.
""",
        encoding="utf-8",
    )


def _normalize(value, *, key: str = ""):
    if key in _VOLATILE_TIME_KEYS:
        return "<present>"
    if key in _VOLATILE_DIGEST_KEYS:
        return "<digest>"
    if key in _VOLATILE_ID_KEYS:
        return "<id>"
    if isinstance(value, dict):
        return {name: _normalize(item, key=name) for name, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str) and _ABSOLUTE_PATH.search(value):
        return Path(value.replace("\\", "/")).name
    if isinstance(value, str):
        return _RANDOM_ID.sub("<id>", value)
    return value


def _recorded_call(args: tuple[object, ...]) -> dict:
    positions = []
    for index, arg in enumerate(args):
        item = {"type": type(arg).__name__}
        if isinstance(arg, Path):
            item["name"] = arg.name
        if index == 1:
            # Normalize too: these are absolute paths in the fixture and the
            # transcript must stay free of machine-specific absolute paths while
            # still detecting a changed value.
            item["codex_config"] = {
                name: _normalize(getattr(arg, name)) for name in _CODEX_CONFIG_FIELDS
            }
        positions.append(item)
    return {"arg_count": len(args), "positions": positions}


def _canonical_transcript() -> bytes:
    with tempfile.TemporaryDirectory(prefix="vl-equivalence-") as temp_name:
        root = Path(temp_name)
        memory = root / "MEMORY"
        (memory / "wiki").mkdir(parents=True)
        raw = memory / "raw" / "source.md"
        raw.parent.mkdir(parents=True)
        raw.write_text("stable characterization source\n", encoding="utf-8")

        patch = pytest.MonkeyPatch()
        db_store.close_all_connections()
        wiki_utils._META_DIR_CACHE = None
        patch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory))
        patch.setenv("VECTOR_LAKE_META_DIR", str(memory / "wiki" / ".meta"))
        patch.delenv("VECTOR_LAKE_DB_PATH", raising=False)
        calls: list[tuple[object, ...]] = []
        statuses: list[dict] = []
        error = None
        try:
            _write_config(memory)
            _write_purpose_contract(memory)
            db_store.init_db()
            from vector_lake.tool_ingest import INGEST_CONTRACT_VERSION

            payload = {
                "filepath": str(raw),
                "hash": stable_raw_revision(raw).canonical_revision,
                "canonical_name": "Source_Test.md",
                "instructions": "compile",
                "source_hash": "",
                "source_projection_hash": "",
                "source_observed_at": "2026-08-31T12:00:00+00:00",
                "attempt_id": "1" * 32,
                "integration_candidates": [],
                # Must match the dispatch predicate the controller claims with.
                "ingest_contract_version": INGEST_CONTRACT_VERSION,
            }
            # The controller claims through claim_subagent_jobs, which only takes jobs
            # already dispatched to awaiting_subagent by the ingest worker. So the
            # harness must perform that dispatch step itself, and the payload must
            # carry the CURRENT contract version or the claim predicate rejects it.
            job_id = db_store.enqueue_job("ingest", payload)
            dispatched = db_store.claim_pending_jobs(limit=1, lease_seconds=300)
            assert [row["job_id"] for row in dispatched] == [job_id]
            row = dispatched[0]
            assert db_store.mark_job_awaiting_subagent(
                job_id,
                str(root / "task.json"),
                lease_owner=row["lease_owner"],
                lease_token=row["lease_token"],
                lease_generation=row["lease_generation"],
            )

            fixed_output = auto_ingest_worker._GeneratedOutput(
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "purpose_scope": "excluded",
                    "purpose_evidence": "Outside the active purpose.",
                    "decision_confidence": 0.99,
                    "files": [],
                    "integration": {
                        "disposition": "rejected",
                        "reason": "Outside the active purpose contract.",
                        "relations": [],
                    },
                },
                {"input_tokens": 11, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 1},
            )

            def recorder(*args):
                calls.append(args)
                return fixed_output

            def capture_status(state, task_queue_size, index_queue_size, current_action, last_error, component="watchdog"):
                statuses.append({
                    "component": component,
                    "status": state,
                    "task_queue_size": task_queue_size,
                    "index_queue_size": index_queue_size,
                    "current_action": current_action,
                    "last_error": last_error,
                })
                return True

            patch.setattr(auto_ingest_worker, "_probe_codex_runner", lambda _config: Path("C:/codex.exe"))
            patch.setattr(auto_ingest_worker, "_run_codex_generator", recorder)
            patch.setattr(auto_ingest_worker, "write_status", capture_status)
            # The heavy-task gate is an unrelated runtime resource: a live watchdog
            # holding it made tick return "idle" with no transcript at all. Stubbing
            # it isolates the code path under test. It changes no transcript field.
            patch.setattr(
                auto_ingest_worker,
                "heavy_task",
                lambda *_args, **_kwargs: contextlib.nullcontext(),
            )
            try:
                outcome = auto_ingest_worker.AutoIngestController().tick(threading.Event())
            except Exception as exc:  # transcript failures instead of hiding them
                error = {"type": type(exc).__name__, "message": str(exc)}
                outcome = "raised"

            rows = db_store.get_connection().execute(
                "SELECT stage, transition, error_code, error_fingerprint, duration_ms "
                "FROM ingest_stage_events WHERE job_id = ?",
                (job_id,),
            ).fetchall()
            events = sorted(
                (
                    {
                        "stage": row["stage"],
                        "transition": row["transition"],
                        "error_code": row["error_code"] or "",
                        "error_fingerprint": row["error_fingerprint"] or "",
                        "duration_ms_present": row["duration_ms"] is not None,
                    }
                    for row in rows
                ),
                key=lambda item: (
                    item["stage"], item["transition"], item["error_code"],
                    item["error_fingerprint"], item["duration_ms_present"],
                ),
            )
            receipts = list((memory / "wiki" / ".meta" / "auto_ingest_attempt_receipts").rglob("*.json"))
            assert len(receipts) == 1
            receipt = _normalize(json.loads(receipts[0].read_text(encoding="utf-8")))
            state_path = memory / "wiki" / ".meta" / ".auto_ingest_controller_state.json"
            budget = _normalize(json.loads(state_path.read_text(encoding="utf-8")))
            transcript = {
                "budget": budget,
                "call": _recorded_call(calls[0]) if calls else None,
                "db": {"attempt_receipt": receipt, "stage_events": events},
                "error": error,
                "outcome": outcome,
                "status": _normalize(statuses),
            }
            return (json.dumps(transcript, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        finally:
            patch.undo()
            db_store.close_all_connections()
            wiki_utils._META_DIR_CACHE = None


def test_transcript_contains_no_machine_or_time_values():
    text = _canonical_transcript().decode("utf-8")
    assert not _ABSOLUTE_PATH.search(text)
    assert not _ISO_TIMESTAMP.search(text)
    assert not _RANDOM_ID.search(text)


def test_transcript_is_stable_across_two_runs():
    assert _canonical_transcript() == _canonical_transcript()


def test_transcript_matches_recorded_baseline():
    baseline_name = os.environ.get("VL_TRANSCRIPT_BASELINE")
    if not baseline_name or not Path(baseline_name).is_file():
        pytest.skip("VL_TRANSCRIPT_BASELINE is unset or does not name an existing file")
    assert _canonical_transcript() == Path(baseline_name).read_bytes()


if __name__ == "__main__":
    output_name = os.environ.get("VL_TRANSCRIPT_OUT")
    if not output_name:
        raise SystemExit("VL_TRANSCRIPT_OUT is required")
    data = _canonical_transcript()
    Path(output_name).write_bytes(data)
    print(f"bytes={len(data)} sha256={hashlib.sha256(data).hexdigest()}")
