from unittest.mock import patch

import pytest

from vector_lake import cli_app, mcp_server
from vector_lake.tool_delete import delete_source
from vector_lake.tool_gc import gc_vector_lake, prune_runtime_history


@pytest.mark.parametrize(
    ("command", "tool_name", "expected_kwargs"),
    [
        (["delete", "source.pdf"], "delete_source", {"dry_run": True}),
        (["gc"], "gc_vector_lake", {"days": 30, "dry_run": True}),
    ],
)
def test_destructive_cli_commands_default_to_preview(
    command, tool_name, expected_kwargs
):
    with (
        patch.object(cli_app.tools, tool_name, return_value="preview") as tool,
        patch("sys.argv", ["cli.py", *command]),
    ):
        assert cli_app.main() == 0

    if tool_name == "delete_source":
        tool.assert_called_once_with("source.pdf", **expected_kwargs)
    else:
        tool.assert_called_once_with(**expected_kwargs)


@pytest.mark.parametrize(
    ("command", "tool_name", "expected_kwargs"),
    [
        (
            ["delete", "source.pdf", "--apply"],
            "delete_source",
            {"dry_run": False},
        ),
        (
            ["gc", "--days", "7", "--apply"],
            "gc_vector_lake",
            {"days": 7, "dry_run": False},
        ),
    ],
)
def test_destructive_cli_commands_require_apply(
    command, tool_name, expected_kwargs
):
    with (
        patch.object(cli_app.tools, tool_name, return_value="applied") as tool,
        patch("sys.argv", ["cli.py", *command]),
    ):
        assert cli_app.main() == 0

    if tool_name == "delete_source":
        tool.assert_called_once_with("source.pdf", **expected_kwargs)
    else:
        tool.assert_called_once_with(**expected_kwargs)


@pytest.mark.parametrize("command", ["delete", "gc"])
def test_destructive_cli_keeps_dry_run_flag_and_rejects_conflict(command):
    parser = cli_app.build_parser()
    positional = ["source.pdf"] if command == "delete" else []

    preview = parser.parse_args([command, *positional, "--dry-run"])
    assert preview.dry_run is True
    assert preview.apply is False

    with pytest.raises(SystemExit):
        parser.parse_args([command, *positional, "--dry-run", "--apply"])


@pytest.mark.parametrize("value", ["-1", "0", "1.5", "invalid"])
def test_gc_cli_rejects_invalid_days(value):
    with pytest.raises(SystemExit):
        cli_app.build_parser().parse_args(["gc", "--days", value])


@pytest.mark.parametrize("value", [-1, 0, True, 1.5, "30", None])
def test_gc_tool_and_mcp_reject_invalid_days(value):
    with pytest.raises(ValueError, match="positive integer"):
        gc_vector_lake(days=value)
    with pytest.raises(ValueError, match="positive integer"):
        prune_runtime_history(days=value)
    with pytest.raises(ValueError, match="positive integer"):
        mcp_server.gc_vector_lake(days=value)


def test_delete_uses_the_validated_resolved_path_for_probe_and_removal(
    isolated_memory,
):
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "source.pdf"
    unresolved_input = raw_dir / "nested" / ".." / raw_path.name
    resolved_path = raw_path.resolve()

    with (
        patch(
            "vector_lake.tool_delete.os.path.exists",
            side_effect=[True, True],
        ) as exists,
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        result = delete_source(str(unresolved_input), dry_run=False)

    assert str(resolved_path) in result
    assert [call.args[0] for call in exists.call_args_list] == [
        resolved_path,
        resolved_path,
    ]
    remove.assert_called_once_with(resolved_path)


def test_delete_matches_only_exact_source_identity(isolated_memory):
    raw_path = isolated_memory / "raw" / "foo.pdf"
    raw_path.write_text("raw", encoding="utf-8")
    wiki_dir = isolated_memory / "wiki"

    def write_page(filename, page_type, sources):
        rendered_sources = ", ".join(sources)
        (wiki_dir / filename).write_text(
            "---\n"
            f"id: {filename[:-3]}\n"
            f"title: {filename[:-3]}\n"
            f"type: {page_type}\n"
            f"sources: [{rendered_sources}]\n"
            "---\nbody\n",
            encoding="utf-8",
        )

    write_page("Source_foo.md", "source", [])
    write_page("Source_Foo-Legacy.md", "source", ["raw/foo.pdf"])
    write_page("Concept_Exact.md", "concept", ["raw/foo.pdf", "raw/keep.pdf"])
    write_page("Concept_Sole.md", "concept", ["raw/foo.pdf"])
    write_page("Source_foobar.md", "source", ["raw/foobar.pdf"])
    write_page("Concept_Substring.md", "concept", ["raw/archive/foo.pdf.notes"])

    preview = delete_source(str(raw_path), dry_run=True)

    assert "[WIKI] 4 affected wiki page(s)" in preview
    for expected in (
        "Source_foo.md",
        "Source_Foo-Legacy.md",
        "Concept_Exact.md",
        "Concept_Sole.md",
    ):
        assert expected in preview
    assert "Source_foobar.md" not in preview
    assert "Concept_Substring.md" not in preview

    captured = []
    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch",
            side_effect=lambda mutations: captured.extend(mutations),
        ),
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        applied = delete_source(str(raw_path), dry_run=False)

    by_filename = {item["filename"]: item for item in captured}
    assert set(by_filename) == {
        "Source_foo.md",
        "Source_Foo-Legacy.md",
        "Concept_Exact.md",
        "Concept_Sole.md",
    }
    assert by_filename["Source_foo.md"]["is_delete"] is True
    assert by_filename["Source_Foo-Legacy.md"]["is_delete"] is True
    assert by_filename["Concept_Sole.md"]["is_delete"] is True
    assert "raw/foo.pdf" not in by_filename["Concept_Exact.md"]["content"]
    assert "raw/keep.pdf" in by_filename["Concept_Exact.md"]["content"]
    assert "wiki_deleted=3" in applied
    assert "wiki_updated=1" in applied
    remove.assert_called_once_with(raw_path.resolve())


def test_gc_cli_and_mcp_forward_explicit_orphan_confirmation():
    confirmation = "sha256:" + ("a" * 64)
    with (
        patch.object(
            cli_app.tools,
            "gc_vector_lake",
            return_value="applied",
        ) as cli_tool,
        patch(
            "sys.argv",
            [
                "cli.py",
                "gc",
                "--apply",
                "--confirm-orphans",
                confirmation,
            ],
        ),
    ):
        assert cli_app.main() == 0
    cli_tool.assert_called_once_with(
        days=30,
        dry_run=False,
        orphan_confirmation=confirmation,
    )

    with patch.object(
        mcp_server.tools,
        "gc_vector_lake",
        return_value="applied",
    ) as mcp_tool:
        assert mcp_server.gc_vector_lake(
            days=7,
            dry_run=False,
            orphan_confirmation=confirmation,
        ) == "applied"
    mcp_tool.assert_called_once_with(
        days=7,
        dry_run=False,
        orphan_confirmation=confirmation,
    )


def test_delete_discovers_uppercase_markdown_extension(isolated_memory):
    raw_path = isolated_memory / "raw" / "foo.pdf"
    raw_path.write_text("raw", encoding="utf-8")
    wiki_page = isolated_memory / "wiki" / "Source_foo.MD"
    wiki_page.write_text(
        "---\n"
        "id: source_foo\n"
        "title: Source foo\n"
        "type: source\n"
        "sources: [raw/foo.pdf]\n"
        "---\nbody\n",
        encoding="utf-8",
    )

    preview = delete_source(str(raw_path), dry_run=True)

    assert "Source_foo.MD" in preview
    captured = []
    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch",
            side_effect=lambda mutations: captured.extend(mutations),
        ),
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        applied = delete_source(str(raw_path), dry_run=False)

    assert captured == [{"filename": "Source_foo.MD", "is_delete": True}]
    assert "wiki_deleted=1" in applied
    remove.assert_called_once_with(raw_path.resolve())


def test_delete_blocks_when_any_markdown_frontmatter_is_damaged(
    isolated_memory,
):
    raw_path = isolated_memory / "raw" / "foo.pdf"
    raw_path.write_text("raw", encoding="utf-8")
    wiki_dir = isolated_memory / "wiki"
    (wiki_dir / "Source_foo.md").write_text(
        "---\nid: source_foo\ntype: source\n"
        "sources: [raw/foo.pdf]\n---\nbody\n",
        encoding="utf-8",
    )
    (wiki_dir / "Concept_Damaged.MD").write_text(
        "---\nid: broken\ncategories: [\n---\nbody\n",
        encoding="utf-8",
    )

    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch"
        ) as execute,
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        result = delete_source(str(raw_path), dry_run=False)

    assert "[BLOCKED]" in result
    assert "Concept_Damaged.MD" in result
    assert "cannot parse YAML frontmatter" in result
    assert "raw source was preserved" in result
    assert raw_path.exists()
    execute.assert_not_called()
    remove.assert_not_called()
