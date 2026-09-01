import inspect
import re
from pathlib import Path

import pytest
import yaml

from vector_lake import mcp_server


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"
SKILL_TOOLS = {
    "audit": ("trigger_audit_graph",),
    "check-duplicate": ("check_duplicate_entity",),
    "daemon-watchdog": ("mcp_runtime_status",),
    "debt": ("get_governance_debt",),
    "delete": ("delete_source",),
    "doctor": ("doctor_vector_lake",),
    "gc": ("gc_vector_lake",),
    "graph": ("visualize_vector_lake",),
    "lint": ("lint_vector_lake",),
    "memory-update": ("update_operational_memory",),
    "merge": ("merge_suggestions_vector_lake",),
    "query": ("query_logic_lake",),
    "research": (
        "trigger_autonomous_research",
        "doctor_vector_lake",
        "sync_vector_lake",
    ),
    "resolve": ("review_governance_list", "resolve_governance_item"),
    "review": ("review_governance_list",),
    "search": ("search_vector_lake",),
    "sync": ("sync_vector_lake", "prepare_ingest_batch", "list_ingest_tasks"),
    "timeline": ("search_timeline",),
    "trace": ("trace_vector_lake",),
}
BANNED_SKILL_SURFACES = (
    "<thought>",
    "invoke_subagent",
    "run_command",
    "call_mcp_tool",
)


def _skill_text(name: str) -> str:
    return (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict:
    match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, flags=re.DOTALL)
    assert match is not None
    metadata = yaml.safe_load(match.group(1))
    assert isinstance(metadata, dict)
    return metadata


def test_skill_surface_is_exact_and_uses_current_contract_version():
    packaged = {
        path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md") if path.is_file()
    }

    assert packaged == set(SKILL_TOOLS)
    for name in sorted(packaged):
        text = _skill_text(name)
        metadata = _frontmatter(text)
        contract = metadata["metadata"]
        assert metadata["name"] == name
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
        assert contract["version"] == "11.20.0"
        assert contract["tier"] in {"read-only", "action-allowed"}
        assert metadata["description"]
        for banned in BANNED_SKILL_SURFACES:
            assert banned not in text


@pytest.mark.parametrize(
    ("skill_name", "tool_names"),
    sorted(SKILL_TOOLS.items()),
)
def test_skill_tools_match_callable_mcp_surface(skill_name, tool_names):
    text = _skill_text(skill_name)

    for tool_name in tool_names:
        assert f"`{tool_name}`" in text
        assert callable(getattr(mcp_server, tool_name))


def test_preview_first_skill_defaults_match_implementation():
    defaults = {
        "trigger_audit_graph": ("dry_run", True),
        "delete_source": ("dry_run", True),
        "gc_vector_lake": ("dry_run", True),
        "lint_vector_lake": ("auto_fix", False),
        "merge_suggestions_vector_lake": ("enqueue", False),
        "query_logic_lake": ("dry_run", True),
    }

    for tool_name, (parameter, expected) in defaults.items():
        signature = inspect.signature(getattr(mcp_server, tool_name))
        assert signature.parameters[parameter].default is expected


@pytest.mark.parametrize(
    ("skill_name", "preview_token", "apply_token"),
    [
        ("audit", "dry_run=True", "dry_run=False"),
        ("delete", "dry_run=True", "dry_run=False"),
        ("gc", "dry_run=True", "dry_run=False"),
        ("lint", "auto_fix=False", "auto_fix=True"),
        ("merge", "enqueue=False", "enqueue=True"),
        ("research", "dry_run=True", "dry_run=False"),
    ],
)
def test_mutating_skills_preserve_preview_and_explicit_approval_boundary(
    skill_name,
    preview_token,
    apply_token,
):
    text = _skill_text(skill_name)

    assert preview_token in text
    assert apply_token in text
    assert "explicit" in text.lower()


def test_host_workflow_skills_preserve_the_removed_command_safety_contracts():
    required_tokens = {
        "audit": ("dry_run=True", "fingerprint", "explicit"),
        "daemon-watchdog": (
            "mcp_runtime_status",
            "stale=false",
            "source_root",
            "success",
        ),
        "delete": ("dry_run=True", "approval", "dry_run=False"),
        "gc": (
            "dry_run=True",
            "fingerprint",
            "orphan_confirmation",
            "dry_run=False",
        ),
        "graph": ("approve", "output_dir", "exists"),
        "lint": ("auto_fix=False", "explicit", "auto_fix=True"),
        "merge": ("enqueue=False", "explicit", "enqueue=True"),
        "query": ("dry_run: true", "do not create"),
        "research": ("dry_run=True", "explicit", "dry_run=False"),
        "resolve": ("review_governance_list", "explicit", "exact"),
        "review": ("read-only", "do not"),
        "search": ("`page`", "`memory`", "`fact`", "deprecated"),
        "sync": ("scan configured roots and enqueue", "does not mean"),
    }

    for skill_name, tokens in required_tokens.items():
        contract = _skill_text(skill_name).casefold()
        assert all(token.casefold() in contract for token in tokens), skill_name


@pytest.mark.parametrize("skill_name", ["memory-update", "resolve"])
def test_payload_mutations_require_exact_payload_and_explicit_approval(skill_name):
    text = _skill_text(skill_name).lower()

    assert "exact" in text
    assert "payload" in text
    assert "explicit" in text


def test_search_skill_uses_fact_mode_without_overstating_claim_semantics():
    text = _skill_text("search")

    assert "`page`, `memory`, or `fact`" in text
    assert "deprecated compatibility alias for `fact`" in text
    assert "not canonical Claim records" in text
    assert "`preference`, `decision`, and `task_state`" in text


def test_authority_research_and_sync_skills_fail_closed_on_known_drifts():
    daemon = _skill_text("daemon-watchdog")
    research = _skill_text("research")
    sync = _skill_text("sync")

    assert "source_root" in daemon
    assert "stale=false" in daemon
    assert "USERPROFILE" not in daemon
    assert ".codex\\plugins\\vector-lake" not in daemon
    assert "<effective MEMORY>/raw/research" in research
    assert "C:\\Users\\shich\\.gemini" not in research
    assert "~/.gemini" not in research
    assert "scan configured roots and enqueue bounded ingest jobs" in sync
    assert "does not mean" in sync


@pytest.mark.parametrize(
    "relative_path",
    ["reset_jobs.py", "check_jobs.py", "scripts/launch_janitor_swarm.py"],
)
def test_unsupported_maintenance_entrypoints_are_not_packaged(relative_path):
    assert not (ROOT / relative_path).exists()
