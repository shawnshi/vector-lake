import tomllib
from pathlib import Path

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


def test_memory_cleanup_is_preview_first_across_cli_and_mcp():
    from vector_lake.cli_app import build_parser

    args = build_parser().parse_args(["memory-cleanup"])
    assert args.apply is False
    assert args.limit == 0
    assert callable(mcp_server.operational_memory_cleanup)


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
