from pathlib import Path

import pytest

from vector_lake import get_extension_root
from vector_lake.native_llm import (
    SubagentTaskRequired,
    generate_json_array,
    get_subagent_scratch_dir,
    get_subagent_task_root,
    native_llm_ready,
)


ROOT = Path(__file__).resolve().parents[1]


def test_text_generation_boundary_does_not_call_generate_content():
    source = (ROOT / "vector_lake" / "native_llm.py").read_text(encoding="utf-8")
    assert "generate_content" not in source
    assert "genai.Client" not in source


def test_generate_json_array_creates_subagent_packet(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-text-boundary")
    ok, detail = native_llm_ready()
    assert ok is False
    assert "subagent" in detail

    with pytest.raises(SubagentTaskRequired) as exc:
        generate_json_array("return []")

    task_path = exc.value.task_path
    task_root = get_subagent_task_root().resolve()
    assert task_path.exists()
    assert task_path.resolve().is_relative_to(task_root)
    assert not task_path.resolve().is_relative_to(get_extension_root().resolve())
    assert task_path.parent.name == "pytest-text-boundary"
    task_path.unlink()


def test_runtime_roots_follow_active_database_and_not_extension():
    from vector_lake.db_store import peek_db_path

    database_parent = peek_db_path().resolve().parent
    task_root = get_subagent_task_root().resolve()
    scratch = get_subagent_scratch_dir().resolve()

    assert task_root.parent == database_parent
    assert scratch.parents[2] == database_parent
    assert not task_root.is_relative_to(get_extension_root().resolve())
    assert not scratch.is_relative_to(get_extension_root().resolve())


def test_task_root_rejects_relative_or_versioned_overrides(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_TASK_ROOT", "relative-task-root")
    with pytest.raises(ValueError, match="absolute path"):
        get_subagent_task_root()

    monkeypatch.setenv(
        "VECTOR_LAKE_SUBAGENT_TASK_ROOT",
        str(get_extension_root() / "runtime-task-root"),
    )
    with pytest.raises(ValueError, match="versioned extension root"):
        get_subagent_task_root()
