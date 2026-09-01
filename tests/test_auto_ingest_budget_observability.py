from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from vector_lake import auto_ingest_worker
from vector_lake import tool_auto_ingest
from vector_lake import cli_app, mcp_server, wiki_utils
from vector_lake.tool_auto_ingest import (
    auto_ingest_attempt_receipt_retention,
    auto_ingest_budget_status,
)


def _launch(at: datetime, index: int, *, reserved: int = 32768) -> dict:
    return {
        "at": at.isoformat(),
        "revision": f"{index:064x}",
        "reserved_tokens": reserved,
        "job_id": f"job-{index:04d}",
        "attempt_id": f"{index:032x}",
    }


def _write_state(launches: list[dict]) -> None:
    state = auto_ingest_worker._empty_state()
    state["launches"] = launches
    auto_ingest_worker._save_state(state)


def _write_receipt(launch: dict, *, outcome: str, usage: dict[str, int]) -> None:
    started = datetime.fromisoformat(launch["at"])
    path = (
        auto_ingest_worker._attempt_receipt_root()
        / started.date().isoformat()
        / f"{launch['attempt_id']}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "attempt_id": launch["attempt_id"],
                "job_id": launch["job_id"],
                "revision": launch["revision"],
                "reserved_tokens": launch["reserved_tokens"],
                "started_at": launch["at"],
                "ended_at": (started + timedelta(minutes=1)).isoformat(),
                "outcome": outcome,
                "usage": usage,
            }
        ),
        encoding="utf-8",
    )


def test_budget_status_reports_exact_100_and_2000_reservation_boundaries(
    isolated_memory,
):
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    launches = [
        _launch(now - timedelta(minutes=30), index) for index in range(100)
    ] + [
        _launch(now - timedelta(hours=2), index)
        for index in range(100, 2000)
    ]
    _write_state(launches)

    report = auto_ingest_budget_status(now=now)

    assert report["complete"] is False
    assert report["reservation_ledger_complete"] is True
    assert report["status"] == "degraded"
    assert report["hour"] == {
        "launches": 100,
        "launches_remaining": 0,
        "reserved_tokens": 3_276_800,
        "reserved_tokens_remaining": 4_915_200,
        "next_release_at": (now + timedelta(minutes=30)).isoformat(),
    }
    assert report["rolling_24h"]["launches"] == 2000
    assert report["rolling_24h"]["launches_remaining"] == 0
    assert report["rolling_24h"]["reserved_tokens"] == 65_536_000
    assert report["rolling_24h"]["reserved_tokens_remaining"] == 0
    assert report["actual_usage"]["complete"] is False
    assert report["actual_usage"]["receipt_states"]["missing"] == 2000
    assert report["receipt_issue_counts"] == {"attempt_receipt_missing": 2000}
    assert len(report["issues"]) == 21
    assert "receipt_issue_details_omitted:1980" in report["issues"]
    assert report["circuit"]["is_open"] is False


def test_budget_status_rejects_task_token_limit_above_hard_ceiling(
    isolated_memory,
):
    auto_ingest_worker._config_path().write_text(
        json.dumps(
            {
                "schema_version": 1,
                "max_tokens_per_task": 81921,
            }
        ),
        encoding="utf-8",
    )

    report = auto_ingest_budget_status(
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    )

    assert report["status"] == "blocked"
    assert report["complete"] is False
    assert len(report["issues"]) == 1
    assert "auto_ingest_config_invalid:max_tokens_per_task" in report["issues"][0]


def test_budget_status_binds_usage_to_exact_receipts_and_marks_partial(
    isolated_memory,
):
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    launches = [_launch(now - timedelta(minutes=index + 1), index) for index in range(3)]
    _write_state(launches)
    _write_receipt(
        launches[0], outcome="finalized", usage={"input_tokens": 10, "output_tokens": 2}
    )
    _write_receipt(launches[1], outcome="started", usage={})
    _write_receipt(
        {**launches[2], "revision": "f" * 64},
        outcome="finalized",
        usage={"input_tokens": 99},
    )

    report = auto_ingest_budget_status(now=now)

    assert report["status"] == "degraded"
    assert report["actual_usage"] == {
        "complete": False,
        "requested": True,
        "totals": {"input_tokens": 10, "output_tokens": 2},
        "receipt_states": {
            "complete": 1,
            "pending": 1,
            "missing": 0,
            "invalid": 1,
        },
    }
    assert report["issues"] == [
        f"attempt_receipt_binding_invalid:{launches[2]['attempt_id']}"
    ]
    assert report["receipt_issue_counts"] == {
        "attempt_receipt_binding_invalid": 1
    }


def test_receipt_retention_is_bounded_fingerprint_confirmed_and_preserves_started(
    isolated_memory,
):
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=15)
    terminal = _launch(old, 1)
    started = _launch(old, 2)
    _write_receipt(terminal, outcome="finalized", usage={"total_tokens": 3})
    _write_receipt(started, outcome="started", usage={})
    terminal_path = next(
        auto_ingest_worker._attempt_receipt_root().rglob(
            f"{terminal['attempt_id']}.json"
        )
    )
    started_path = next(
        auto_ingest_worker._attempt_receipt_root().rglob(
            f"{started['attempt_id']}.json"
        )
    )

    preview = auto_ingest_attempt_receipt_retention(
        plan_as_of=now.isoformat(), limit=1
    )
    assert preview["selected"] == 1
    assert preview["started_preserved"] == 1
    assert preview["has_more"] is True
    assert terminal_path.exists() and started_path.exists()

    with pytest.raises(PermissionError, match="fingerprint"):
        auto_ingest_attempt_receipt_retention(
            apply=True,
            plan_as_of=preview["plan_as_of"],
            limit=1,
        )
    applied = auto_ingest_attempt_receipt_retention(
        apply=True,
        plan_as_of=preview["plan_as_of"],
        limit=1,
        confirm_fingerprint=preview["fingerprint"],
    )
    assert applied["deleted"] == 1
    assert applied["removed_empty_buckets"] == 0
    assert not terminal_path.exists()
    assert started_path.exists()


def test_receipt_retention_validates_whole_batch_before_first_delete(
    isolated_memory, monkeypatch
):
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    launches = [_launch(now - timedelta(days=15), index) for index in (11, 12)]
    for launch in launches:
        _write_receipt(launch, outcome="finalized", usage={"total_tokens": 3})
    paths = sorted(
        next(auto_ingest_worker._attempt_receipt_root().iterdir()).glob("*.json")
    )
    preview = auto_ingest_attempt_receipt_retention(
        plan_as_of=now.isoformat(), limit=2
    )

    original_hash = tool_auto_ingest._file_sha256
    hash_calls = 0

    def mutate_second_after_apply_plan(path):
        nonlocal hash_calls
        hash_calls += 1
        digest = original_hash(path)
        if hash_calls == 3:
            paths[1].write_text("{}", encoding="utf-8")
        return digest

    monkeypatch.setattr(
        tool_auto_ingest, "_file_sha256", mutate_second_after_apply_plan
    )
    with pytest.raises(RuntimeError, match="changed after preview"):
        auto_ingest_attempt_receipt_retention(
            apply=True,
            plan_as_of=preview["plan_as_of"],
            limit=2,
            confirm_fingerprint=preview["fingerprint"],
        )

    assert all(path.exists() for path in paths)


def test_receipt_retention_failure_has_durable_idempotent_resume(
    isolated_memory, monkeypatch
):
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    launches = [_launch(now - timedelta(days=15), index) for index in (21, 22)]
    for launch in launches:
        _write_receipt(launch, outcome="finalized", usage={"total_tokens": 3})
    paths = sorted(
        next(auto_ingest_worker._attempt_receipt_root().iterdir()).glob("*.json")
    )
    preview = auto_ingest_attempt_receipt_retention(
        plan_as_of=now.isoformat(), limit=2
    )
    real_unlink = tool_auto_ingest._unlink_receipt_candidate
    calls = 0

    def fail_second(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected unlink failure")
        return real_unlink(path)

    monkeypatch.setattr(
        tool_auto_ingest,
        "_unlink_receipt_candidate",
        fail_second,
    )
    with pytest.raises(RuntimeError, match="receipt retention apply failed"):
        auto_ingest_attempt_receipt_retention(
            apply=True,
            plan_as_of=preview["plan_as_of"],
            limit=2,
            confirm_fingerprint=preview["fingerprint"],
        )

    operation_path = tool_auto_ingest._retention_operation_path(
        preview["fingerprint"]
    )
    failed = json.loads(operation_path.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["deleted"] == 1
    assert sum(path.exists() for path in paths) == 1

    monkeypatch.setattr(
        tool_auto_ingest,
        "_unlink_receipt_candidate",
        real_unlink,
    )
    resumed = auto_ingest_attempt_receipt_retention(
        apply=True,
        plan_as_of=preview["plan_as_of"],
        limit=2,
        confirm_fingerprint=preview["fingerprint"],
    )
    repeated = auto_ingest_attempt_receipt_retention(
        apply=True,
        plan_as_of=preview["plan_as_of"],
        limit=2,
        confirm_fingerprint=preview["fingerprint"],
    )

    assert resumed["deleted"] == 2
    assert resumed["resumed"] is True
    assert resumed["idempotent"] is False
    assert repeated["deleted"] == 2
    assert repeated["resumed"] is True
    assert repeated["idempotent"] is True
    assert not any(path.exists() for path in paths)
    completed = json.loads(operation_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"


def test_budget_observability_is_public_but_retention_is_governed():
    parser = cli_app.build_parser()
    status_args = parser.parse_args(["auto-ingest-budget-status"])
    retention_args = parser.parse_args(["auto-ingest-receipt-retention"])

    assert status_args.reservations_only is False
    assert retention_args.apply is False
    assert cli_app._cli_heavy_task_policy(status_args) is None
    assert cli_app._cli_heavy_task_policy(retention_args) == (
        "maintenance",
        900.0,
    )
    assert "auto_ingest_budget_status" in mcp_server._MEMORY_MCP_SURFACE_TOOLS
    assert "auto_ingest_budget_status" in mcp_server._READONLY_MCP_SURFACE_TOOLS
    assert (
        "auto_ingest_receipt_retention"
        not in mcp_server._READONLY_MCP_SURFACE_TOOLS
    )
    assert mcp_server._MCP_HEAVY_TASKS["auto_ingest_receipt_retention"] == (
        "maintenance",
        900.0,
    )
    assert mcp_server._MCP_HEAVY_TASKS["auto_ingest_budget_status"] == (
        "scan",
        900.0,
    )


def test_readonly_budget_allowlist_call_does_not_touch_meta_tree(
    isolated_memory, monkeypatch
):
    meta_dir = isolated_memory / "wiki" / ".meta"
    meta_dir.mkdir()
    sentinel = meta_dir / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    wiki_utils._META_DIR_CACHE = None
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "readonly")

    def tree_fingerprint():
        return tuple(
            sorted(
                (
                    path.relative_to(meta_dir).as_posix(),
                    path.is_dir(),
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in meta_dir.rglob("*")
            )
        )

    before_mtime_ns = meta_dir.stat().st_mtime_ns
    before_tree = tree_fingerprint()

    assert "auto_ingest_budget_status" in mcp_server._READONLY_MCP_SURFACE_TOOLS
    report = json.loads(mcp_server.auto_ingest_budget_status())

    assert report["reservation_ledger_complete"] is True
    assert meta_dir.stat().st_mtime_ns == before_mtime_ns
    assert tree_fingerprint() == before_tree
    assert wiki_utils._META_DIR_CACHE is None


def test_reservation_only_status_never_reads_attempt_receipts(
    isolated_memory, monkeypatch
):
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    _write_state([_launch(now - timedelta(minutes=5), 1)])
    monkeypatch.setattr(
        tool_auto_ingest,
        "_verified_receipt_usage",
        lambda _launch: pytest.fail("receipt read was not skipped"),
    )

    report = auto_ingest_budget_status(now=now, include_actual_usage=False)

    assert report["complete"] is True
    assert report["reservation_ledger_complete"] is True
    assert report["actual_usage"]["requested"] is False
