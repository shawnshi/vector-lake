import tomllib
from pathlib import Path
import pytest


from vector_lake import mcp_server


ROOT = Path(__file__).resolve().parents[1]


def _load_command(name: str) -> dict:
    with (ROOT / "commands" / f"{name}.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_query_and_timeline_compatibility_commands_match_mcp_tools():
    query = _load_command("query")
    timeline = _load_command("timeline")

    assert "query_logic_lake" in query["prompt"]
    assert "search_timeline" in timeline["prompt"]
    assert callable(mcp_server.query_logic_lake)
    assert callable(mcp_server.search_timeline)


def test_query_and_timeline_codex_skills_are_packaged():
    assert (ROOT / "skills" / "query" / "SKILL.md").is_file()
    assert (ROOT / "skills" / "timeline" / "SKILL.md").is_file()


def test_cbss_evidence_and_semantic_readiness_surfaces_are_registered():
    assert callable(mcp_server.export_evidence_packet)
    assert callable(mcp_server.semantic_readiness)
    assert callable(mcp_server.sync_critical_decision_registry)


def test_readiness_cli_accepts_verified_decision_scope():
    from vector_lake.cli_app import build_parser

    args = build_parser().parse_args(["readiness", "--decision-id", "CD-001"])
    assert args.decision_id == "CD-001"


def test_memory_search_index_is_explicit_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    preview = build_parser().parse_args(["memory-search-index"])
    apply = build_parser().parse_args(
        [
            "memory-search-index",
            "--apply",
            "--batch-size",
            "32",
        ]
    )

    assert preview.apply is False
    assert preview.batch_size == 256
    assert apply.apply is True
    assert apply.batch_size == 32
    assert callable(mcp_server.operational_memory_search_index)


def test_memory_cleanup_is_preview_first_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    args = build_parser().parse_args(["memory-cleanup"])
    assert args.apply is False
    assert args.limit == 0
    assert callable(mcp_server.operational_memory_cleanup)


def test_orphan_ingest_packet_cleanup_is_preview_first_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    args = build_parser().parse_args(["ingest-tasks", "--cleanup-orphans"])
    assert args.apply is False
    assert args.limit == 20
    assert args.min_age_seconds == 86400
    assert callable(mcp_server.reconcile_orphan_ingest_packets)


@pytest.mark.parametrize("flag", ["--limit", "--min-age-seconds"])
def test_orphan_ingest_packet_cleanup_rejects_negative_bounds(flag):
    from vector_lake.cli_app import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["ingest-tasks", "--cleanup-orphans", flag, "-1"])

    kwargs = {"dry_run": True, "limit": 0, "min_age_seconds": 0}
    kwargs["limit" if flag == "--limit" else "min_age_seconds"] = -1
    with pytest.raises(ValueError):
        mcp_server.reconcile_orphan_ingest_packets(**kwargs)


def test_evidence_foundation_backfill_is_preview_first_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    args = build_parser().parse_args(["evidence-foundation-backfill"])
    assert args.apply is False
    assert args.limit == 500
    assert args.batch_size == 100
    assert callable(mcp_server.evidence_foundation_backfill)


def test_orphan_source_classification_is_preview_first_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    args = build_parser().parse_args(["orphan-source-classify"])
    assert args.apply is False
    assert callable(mcp_server.orphan_source_classify)


def test_backup_retention_is_preview_first_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    preview = build_parser().parse_args(["backup-retention"])
    apply = build_parser().parse_args(
        [
            "backup-retention",
            "--apply",
            "--keep-latest",
            "3",
            "--min-age-days",
            "45",
            "--stage-ttl-hours",
            "12",
            "--confirm-fingerprint",
            "sha256:abc",
        ]
    )

    assert preview.apply is False
    assert preview.keep_latest == 5
    assert preview.min_age_days == 30
    assert preview.stage_ttl_hours == 24
    assert preview.confirm_fingerprint == ""
    assert apply.apply is True
    assert apply.keep_latest == 3
    assert apply.min_age_days == 45
    assert apply.stage_ttl_hours == 12
    assert apply.confirm_fingerprint == "sha256:abc"
    assert callable(mcp_server.backup_retention)


def test_backup_retention_mcp_forwards_preview_and_explicit_apply(monkeypatch):
    calls = []

    def fake_maintenance(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(
        mcp_server.tools,
        "backup_retention_maintenance",
        fake_maintenance,
    )

    assert mcp_server.backup_retention() == "ok"
    assert (
        mcp_server.backup_retention(
            dry_run=False,
            keep_latest=2,
            confirmation="sha256:abc",
        )
        == "ok"
    )
    assert calls[0] == {
        "dry_run": True,
        "keep_latest": 5,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
        "confirmation": "",
    }
    assert calls[1]["dry_run"] is False
    assert calls[1]["keep_latest"] == 2
    assert calls[1]["confirmation"] == "sha256:abc"


def test_history_retention_is_preview_first_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    preview = build_parser().parse_args(["history-retention"])
    apply = build_parser().parse_args(
        [
            "history-retention",
            "--apply",
            "--ttl-days",
            "45",
            "--batch-size",
            "250",
            "--keep-change-sets",
            "10",
            "--keep-terminal-jobs",
            "20",
            "--keep-terminal-outbox",
            "30",
            "--keep-versions-per-family",
            "3",
        ]
    )

    assert preview.apply is False
    assert preview.ttl_days == 30
    assert preview.batch_size == 500
    assert preview.keep_change_sets == 1000
    assert preview.keep_terminal_jobs == 1000
    assert preview.keep_terminal_outbox == 1000
    assert preview.keep_versions_per_family == 2
    assert apply.apply is True
    assert apply.ttl_days == 45
    assert apply.batch_size == 250
    assert apply.keep_change_sets == 10
    assert apply.keep_terminal_jobs == 20
    assert apply.keep_terminal_outbox == 30
    assert apply.keep_versions_per_family == 3
    assert callable(mcp_server.history_retention)


def test_history_retention_mcp_forwards_preview_and_explicit_apply(monkeypatch):
    calls = []

    def fake_maintenance(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(
        mcp_server.tools,
        "history_retention_maintenance",
        fake_maintenance,
    )

    assert mcp_server.history_retention() == "ok"
    assert mcp_server.history_retention(dry_run=False, ttl_days=7) == "ok"
    assert calls[0] == {
        "dry_run": True,
        "ttl_days": 30,
        "batch_size": 500,
        "keep_change_sets": 1000,
        "keep_terminal_jobs": 1000,
        "keep_terminal_outbox": 1000,
        "keep_versions_per_family": 2,
    }
    assert calls[1]["dry_run"] is False
    assert calls[1]["ttl_days"] == 7
