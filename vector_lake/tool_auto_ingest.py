"""Read-only automatic-ingest budget telemetry and bounded receipt retention."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from vector_lake import auto_ingest_worker
from vector_lake.cancellation import cancellation_checkpoint, non_interruptible_phase
from vector_lake.durability import (
    durable_replace_file,
    sync_directory,
    sync_open_file,
)


_BUDGET_STATUS_CONTRACT = "vector-lake-auto-ingest-budget-status/v1"
_RECEIPT_RETENTION_CONTRACT = "vector-lake-auto-ingest-receipt-retention/v1"
_RETENTION_OPERATION_CONTRACT = (
    "vector-lake-auto-ingest-receipt-retention-operation/v1"
)
_MAX_RETENTION_BATCH = 500
_MAX_RECEIPT_BUCKETS = 4096
_MAX_RECEIPTS_PER_BUCKET = 4096
_MAX_ISSUE_DETAILS = 20
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
_DATE_BUCKET = re.compile(r"\d{4}-\d{2}-\d{2}")
_TERMINAL_ATTEMPT_OUTCOMES = frozenset(
    {
        "finalized",
        "finalized_with_warning",
        "infrastructure_error",
        "quarantined",
        "requeued",
        "state_reservation_failed",
        "stopping",
    }
)


def _utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _configured_budget_policy() -> dict[str, Any]:
    """Read budget fields even while the execution host is disabled."""
    defaults = auto_ingest_worker.AutoIngestConfig()
    fields = {
        "max_tasks_per_hour": (1, auto_ingest_worker._MAX_TASKS_PER_HOUR),
        "max_tasks_per_24h": (1, auto_ingest_worker._MAX_TASKS_PER_24H),
        "max_tokens_per_task": (
            16384,
            auto_ingest_worker._MAX_TOKENS_PER_TASK,
        ),
        "max_reserved_tokens_per_hour": (16384, 13107200),
        "max_reserved_tokens_per_24h": (16384, 65536000),
        "scratch_retention_days": (1, 90),
    }
    path = auto_ingest_worker._config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"auto_ingest_config_invalid:{type(exc).__name__}:{exc}"
            ) from exc
        if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
            raise ValueError("auto_ingest_config_invalid:schema")
        raw = loaded
    enabled = raw.get("enabled", defaults.enabled)
    consent = raw.get(
        "allow_model_processing_raw_text",
        defaults.allow_model_processing_raw_text,
    )
    if not isinstance(enabled, bool) or not isinstance(consent, bool):
        raise ValueError("auto_ingest_config_invalid:authorization_fields")
    policy: dict[str, Any] = {
        "enabled": enabled,
        "allow_model_processing_raw_text": consent,
    }
    for name, (minimum, maximum) in fields.items():
        value = raw.get(name, getattr(defaults, name))
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise ValueError(f"auto_ingest_config_invalid:{name}")
        policy[name] = value
    if policy["max_tasks_per_hour"] > policy["max_tasks_per_24h"]:
        raise ValueError("auto_ingest_config_invalid:hourly_budget_exceeds_24h_budget")
    if policy["max_tokens_per_task"] > policy["max_reserved_tokens_per_hour"]:
        raise ValueError(
            "auto_ingest_config_invalid:task_token_reserve_exceeds_hourly_budget"
        )
    if (
        policy["max_reserved_tokens_per_hour"]
        > policy["max_reserved_tokens_per_24h"]
    ):
        raise ValueError(
            "auto_ingest_config_invalid:hourly_token_budget_exceeds_24h_budget"
        )
    return policy


def _release_time(launches: list[dict[str, Any]], delta: timedelta) -> str:
    timestamps = [_utc(item.get("at")) for item in launches]
    valid = [item for item in timestamps if item is not None]
    if not valid:
        return ""
    return (min(valid) + delta).isoformat()


def _receipt_candidates(launch: dict[str, Any]) -> list[Path]:
    attempt_id = str(launch.get("attempt_id") or "")
    launched_at = _utc(launch.get("at"))
    if not _ATTEMPT_ID.fullmatch(attempt_id) or launched_at is None:
        return []
    root = auto_ingest_worker._attempt_receipt_root()
    return [
        root / (launched_at + timedelta(days=offset)).date().isoformat() / f"{attempt_id}.json"
        for offset in (-1, 0, 1)
    ]


def _verified_receipt_usage(
    launch: dict[str, Any],
) -> tuple[dict[str, int], str, list[str]]:
    attempt_id = str(launch.get("attempt_id") or "")
    candidates = [path for path in _receipt_candidates(launch) if path.is_file()]
    if not candidates:
        return {}, "missing", [f"attempt_receipt_missing:{attempt_id or 'legacy'}"]
    if len(candidates) != 1:
        return {}, "invalid", [f"attempt_receipt_ambiguous:{attempt_id}"]
    path = candidates[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, "invalid", [f"attempt_receipt_invalid:{attempt_id}"]
    expected = {
        "attempt_id": attempt_id,
        "job_id": str(launch.get("job_id") or ""),
        "revision": str(launch.get("revision") or ""),
        "reserved_tokens": int(launch.get("reserved_tokens") or 0),
    }
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or any(payload.get(name) != value for name, value in expected.items())
    ):
        return {}, "invalid", [f"attempt_receipt_binding_invalid:{attempt_id}"]
    outcome = str(payload.get("outcome") or "")
    if outcome == "started":
        return {}, "pending", []
    if outcome not in _TERMINAL_ATTEMPT_OUTCOMES or _utc(
        payload.get("ended_at")
    ) is None:
        return {}, "invalid", [f"attempt_receipt_outcome_invalid:{attempt_id}"]
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}, "invalid", [f"attempt_receipt_usage_invalid:{attempt_id}"]
    normalized: dict[str, int] = {}
    for name, value in usage.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return {}, "invalid", [f"attempt_receipt_usage_invalid:{attempt_id}"]
        normalized[str(name)] = value
    return normalized, "complete", []


def auto_ingest_budget_status(
    *,
    now: datetime | None = None,
    include_actual_usage: bool = True,
) -> dict[str, Any]:
    """Return exact reservation budgets and explicitly qualified actual usage."""
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        policy = _configured_budget_policy()
        state = auto_ingest_worker._load_state(captured_at)
    except Exception as exc:
        return {
            "contract": _BUDGET_STATUS_CONTRACT,
            "captured_at": captured_at.isoformat(),
            "status": "blocked",
            "complete": False,
            "issues": [f"budget_ledger_unavailable:{type(exc).__name__}:{exc}"],
        }

    launches = list(state.get("launches") or [])
    hour_cutoff = captured_at - timedelta(hours=1)
    hourly = [
        item
        for item in launches
        if (_utc(item.get("at")) or datetime.min.replace(tzinfo=timezone.utc))
        >= hour_cutoff
    ]
    hour_reserved = sum(int(item.get("reserved_tokens") or 0) for item in hourly)
    day_reserved = sum(int(item.get("reserved_tokens") or 0) for item in launches)
    actual: dict[str, int] = {}
    receipt_states = {"complete": 0, "pending": 0, "missing": 0, "invalid": 0}
    issues: list[str] = []
    receipt_issue_counts: dict[str, int] = {}
    if include_actual_usage:
        for offset, launch in enumerate(launches):
            if offset % 64 == 0:
                cancellation_checkpoint(
                    f"auto_ingest_budget_status:receipt_batch:{offset // 64}"
                )
            usage, receipt_state, receipt_issues = _verified_receipt_usage(launch)
            receipt_states[receipt_state] += 1
            for issue in receipt_issues:
                code = issue.split(":", 1)[0]
                receipt_issue_counts[code] = receipt_issue_counts.get(code, 0) + 1
                if len(issues) < _MAX_ISSUE_DETAILS:
                    issues.append(issue)
            for name, value in usage.items():
                actual[name] = actual.get(name, 0) + value
        usage_complete = (
            receipt_states["complete"] == len(launches)
            and not receipt_issue_counts
            and not receipt_states["pending"]
        )
        omitted_issues = sum(receipt_issue_counts.values()) - len(issues)
        if omitted_issues:
            issues.append(f"receipt_issue_details_omitted:{omitted_issues}")
    else:
        usage_complete = False
    complete = not include_actual_usage or usage_complete
    circuit_open_until = _utc(state.get("circuit_open_until"))
    return {
        "contract": _BUDGET_STATUS_CONTRACT,
        "captured_at": captured_at.isoformat(),
        "status": "ready" if complete else "degraded",
        "complete": complete,
        "reservation_ledger_complete": True,
        "enabled": bool(policy["enabled"]),
        "allow_model_processing_raw_text": bool(
            policy["allow_model_processing_raw_text"]
        ),
        "limits": {
            name: policy[name]
            for name in (
                "max_tasks_per_hour",
                "max_tasks_per_24h",
                "max_tokens_per_task",
                "max_reserved_tokens_per_hour",
                "max_reserved_tokens_per_24h",
            )
        },
        "hour": {
            "launches": len(hourly),
            "launches_remaining": max(0, policy["max_tasks_per_hour"] - len(hourly)),
            "reserved_tokens": hour_reserved,
            "reserved_tokens_remaining": max(
                0, policy["max_reserved_tokens_per_hour"] - hour_reserved
            ),
            "next_release_at": _release_time(hourly, timedelta(hours=1)),
        },
        "rolling_24h": {
            "launches": len(launches),
            "launches_remaining": max(
                0, policy["max_tasks_per_24h"] - len(launches)
            ),
            "reserved_tokens": day_reserved,
            "reserved_tokens_remaining": max(
                0, policy["max_reserved_tokens_per_24h"] - day_reserved
            ),
            "next_release_at": _release_time(launches, timedelta(hours=24)),
        },
        "actual_usage": {
            "complete": usage_complete,
            "requested": bool(include_actual_usage),
            "totals": dict(sorted(actual.items())),
            "receipt_states": receipt_states,
        },
        "circuit": {
            "consecutive_infra_failures": int(
                state.get("consecutive_infra_failures") or 0
            ),
            "open_until": state.get("circuit_open_until"),
            "is_open": bool(
                circuit_open_until is not None
                and circuit_open_until > captured_at
            ),
        },
        "receipt_issue_counts": dict(sorted(receipt_issue_counts.items())),
        "issues": sorted(set(issues)),
    }


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _retention_operation_root() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "auto_ingest_retention_receipts"


def _retention_operation_path(fingerprint: str) -> Path:
    value = str(fingerprint or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise PermissionError("auto-ingest receipt retention fingerprint is invalid")
    return _retention_operation_root() / f"{value.split(':', 1)[1]}.json"


def _write_retention_operation(path: Path, payload: dict[str, Any]) -> None:
    root = path.parent
    root_preexisted = root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("auto-ingest retention receipt root is unsafe")
    if not root_preexisted:
        sync_directory(root.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            sync_open_file(handle)
        durable_replace_file(temporary, path, source_synced=True)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_retention_operation(path: Path) -> dict[str, Any] | None:
    if path.parent.is_symlink() or (
        path.exists() and (path.is_symlink() or not path.is_file())
    ):
        raise RuntimeError("auto-ingest retention operation receipt is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("auto-ingest retention operation receipt is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("contract") != _RETENTION_OPERATION_CONTRACT
        or payload.get("schema_version") != 1
        or payload.get("status") not in {"pending", "failed", "completed"}
        or not isinstance(payload.get("plan"), dict)
        or payload.get("fingerprint") != payload["plan"].get("fingerprint")
    ):
        raise RuntimeError("auto-ingest retention operation receipt is invalid")
    return payload


def _unlink_receipt_candidate(path: Path) -> None:
    path.unlink()


def _retention_operation_result(
    operation: dict[str, Any],
    *,
    idempotent: bool,
    resumed: bool,
) -> dict[str, Any]:
    return {
        **dict(operation["plan"]),
        "applied": operation["status"] == "completed",
        "deleted": int(operation.get("deleted") or 0),
        "removed_empty_buckets": int(
            operation.get("removed_empty_buckets") or 0
        ),
        "resumed": bool(resumed),
        "idempotent": bool(idempotent),
        "operation_receipt": str(
            _retention_operation_path(str(operation["fingerprint"]))
        ),
    }


def _retention_plan(
    *,
    plan_as_of: datetime,
    retention_days: int,
    limit: int,
) -> dict[str, Any]:
    root = auto_ingest_worker._attempt_receipt_root()
    cutoff = plan_as_of - timedelta(days=retention_days)
    candidates: list[dict[str, Any]] = []
    protected_started = 0
    invalid = 0
    scanned = 0
    scanned_buckets = 0
    scan_complete = True
    issues: list[str] = []
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        issues.append("attempt_receipt_root_is_not_a_plain_directory")
    if root.is_dir() and not root.is_symlink():
        for bucket in sorted(root.iterdir(), key=lambda item: item.name):
            cancellation_checkpoint(
                f"auto_ingest_receipt_retention:bucket:{scanned_buckets}"
            )
            if not _DATE_BUCKET.fullmatch(bucket.name):
                continue
            scanned_buckets += 1
            if scanned_buckets > _MAX_RECEIPT_BUCKETS:
                issues.append("attempt_receipt_bucket_scan_limit_exceeded")
                scan_complete = False
                break
            if not bucket.is_dir() or bucket.is_symlink():
                issues.append(f"unsafe_receipt_bucket:{bucket.name}")
                continue
            entries: list[Path] = []
            with os.scandir(bucket) as iterator:
                for entry in iterator:
                    if len(entries) >= _MAX_RECEIPTS_PER_BUCKET:
                        issues.append(f"receipt_bucket_limit_exceeded:{bucket.name}")
                        break
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json"):
                        entries.append(Path(entry.path))
            for path in sorted(entries, key=lambda item: item.name):
                scanned += 1
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    invalid += 1
                    continue
                ended_at = _utc(payload.get("ended_at")) if isinstance(payload, dict) else None
                outcome = str(payload.get("outcome") or "") if isinstance(payload, dict) else ""
                if outcome == "started" or ended_at is None:
                    protected_started += 1
                    continue
                if (
                    payload.get("schema_version") != 1
                    or str(payload.get("attempt_id") or "") != path.stem
                    or outcome not in _TERMINAL_ATTEMPT_OUTCOMES
                ):
                    invalid += 1
                    continue
                if ended_at >= cutoff:
                    continue
                if len(candidates) < limit:
                    candidates.append(
                        {
                            "relative_path": path.relative_to(root).as_posix(),
                            "sha256": _file_sha256(path),
                            "ended_at": ended_at.isoformat(),
                            "outcome": outcome,
                        }
                    )
                else:
                    scan_complete = False
            if len(candidates) >= limit:
                # Finish the current bounded bucket so protected/invalid counts
                # are meaningful, then let a subsequent fingerprinted batch
                # continue with the remaining buckets.
                scan_complete = False
                break
    binding = {
        "contract": _RECEIPT_RETENTION_CONTRACT,
        "plan_as_of": plan_as_of.isoformat(),
        "retention_days": retention_days,
        "limit": limit,
        "candidates": candidates,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **binding,
        "fingerprint": fingerprint,
        "selected": len(candidates),
        "scanned": scanned,
        "scanned_buckets": min(scanned_buckets, _MAX_RECEIPT_BUCKETS),
        "scan_complete": scan_complete,
        "has_more": not scan_complete,
        "invalid_preserved": invalid,
        "started_preserved": protected_started,
        "issues": issues,
        "can_apply": not issues,
    }


def auto_ingest_attempt_receipt_retention(
    *,
    apply: bool = False,
    confirm_fingerprint: str = "",
    plan_as_of: str = "",
    limit: int = 256,
) -> dict[str, Any]:
    """Preview or delete one fingerprint-bound batch of expired terminal receipts."""
    bounded_limit = max(1, min(_MAX_RETENTION_BATCH, int(limit)))
    policy = _configured_budget_policy()
    as_of = _utc(plan_as_of) if plan_as_of else datetime.now(timezone.utc)
    if as_of is None:
        raise ValueError("plan_as_of must be a timezone-aware ISO timestamp")
    operation_path = None
    existing_operation = None
    if apply and confirm_fingerprint:
        operation_path = _retention_operation_path(confirm_fingerprint)
        existing_operation = _load_retention_operation(operation_path)
    if existing_operation is None:
        plan = _retention_plan(
            plan_as_of=as_of,
            retention_days=int(policy["scratch_retention_days"]),
            limit=bounded_limit,
        )
    else:
        plan = dict(existing_operation["plan"])
        if (
            existing_operation.get("fingerprint") != confirm_fingerprint
            or plan.get("plan_as_of") != as_of.isoformat()
            or plan.get("retention_days") != int(policy["scratch_retention_days"])
            or plan.get("limit") != bounded_limit
            or not plan.get("can_apply")
        ):
            raise RuntimeError(
                "auto-ingest retention operation receipt does not match this request"
            )
        if existing_operation["status"] == "completed":
            return _retention_operation_result(
                existing_operation,
                idempotent=True,
                resumed=True,
            )
    if not apply:
        return {
            **plan,
            "applied": False,
            "deleted": 0,
            "resumed": False,
            "idempotent": False,
        }
    if not plan["can_apply"]:
        raise RuntimeError("auto-ingest receipt retention plan is not safe to apply")
    if not confirm_fingerprint or confirm_fingerprint != plan["fingerprint"]:
        raise PermissionError("auto-ingest receipt retention fingerprint is required")
    operation_path = operation_path or _retention_operation_path(
        confirm_fingerprint
    )
    root = auto_ingest_worker._attempt_receipt_root()
    validated_paths: list[Path] = []
    for item in plan["candidates"]:
        path = root / str(item["relative_path"])
        if path.exists() and (
            not path.is_file()
            or path.is_symlink()
            or _file_sha256(path) != item["sha256"]
        ):
            raise RuntimeError("auto-ingest receipt changed after preview")
        if not path.exists() and existing_operation is None:
            raise RuntimeError("auto-ingest receipt changed after preview")
        validated_paths.append(path)

    # Validate the complete batch before deleting its first member.  Terminal
    # receipts are immutable by contract, but this two-phase check keeps an
    # external modification from turning a stale plan into a partially applied
    # retention batch.
    now_text = datetime.now(timezone.utc).isoformat()
    operation = existing_operation or {
        "contract": _RETENTION_OPERATION_CONTRACT,
        "schema_version": 1,
        "fingerprint": confirm_fingerprint,
        "status": "pending",
        "created_at": now_text,
        "updated_at": now_text,
        "completed_at": None,
        "deleted": 0,
        "removed_empty_buckets": 0,
        "error_type": None,
        "plan": plan,
    }
    operation.update(
        {
            "status": "pending",
            "updated_at": now_text,
            "error_type": None,
        }
    )
    _write_retention_operation(operation_path, operation)
    cancellation_checkpoint("auto_ingest_receipt_retention:before_apply")
    resumed = existing_operation is not None
    try:
        with non_interruptible_phase("auto_ingest_receipt_retention"):
            for item, path in zip(plan["candidates"], validated_paths, strict=True):
                if not path.exists():
                    continue
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or _file_sha256(path) != item["sha256"]
                ):
                    raise RuntimeError(
                        "auto-ingest receipt changed during retention apply"
                    )
                _unlink_receipt_candidate(path)

            buckets = sorted({path.parent for path in validated_paths})
            for bucket in buckets:
                if bucket.is_dir() and not bucket.is_symlink():
                    sync_directory(bucket)
            removed_empty_buckets = 0
            for bucket in buckets:
                try:
                    bucket.rmdir()
                except OSError:
                    continue
                removed_empty_buckets += 1
            if root.is_dir() and not root.is_symlink():
                sync_directory(root)

            completed_at = datetime.now(timezone.utc).isoformat()
            operation.update(
                {
                    "status": "completed",
                    "updated_at": completed_at,
                    "completed_at": completed_at,
                    "deleted": len(validated_paths),
                    "removed_empty_buckets": removed_empty_buckets,
                    "error_type": None,
                }
            )
            _write_retention_operation(operation_path, operation)
    except BaseException as exc:
        operation.update(
            {
                "status": "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "deleted": sum(not path.exists() for path in validated_paths),
                "error_type": type(exc).__name__,
            }
        )
        try:
            _write_retention_operation(operation_path, operation)
        except BaseException as receipt_exc:
            raise RuntimeError(
                "auto-ingest receipt retention apply failed and its failure "
                "receipt could not be published"
            ) from receipt_exc
        raise RuntimeError("auto-ingest receipt retention apply failed") from exc
    return _retention_operation_result(
        operation,
        idempotent=False,
        resumed=resumed,
    )


__all__ = [
    "auto_ingest_attempt_receipt_retention",
    "auto_ingest_budget_status",
]
