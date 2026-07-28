from pathlib import Path
from unittest.mock import patch

import pytest

from vector_lake import cli_app, mcp_server
from vector_lake.tool_delete import delete_source
from vector_lake.tool_gc import gc_vector_lake, prune_runtime_history


def _assert_quarantine_removed(remove, raw_path):
    remove.assert_called_once()
    quarantine_path = Path(remove.call_args.args[0])
    assert quarantine_path.parent == raw_path.resolve().parent
    assert quarantine_path.name.startswith(f".{raw_path.name}.vector-lake-delete-")
    assert quarantine_path.name.endswith(".quarantine")


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
def test_destructive_cli_commands_require_apply(command, tool_name, expected_kwargs):
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
    raw_path.write_text("raw", encoding="utf-8")
    unresolved_input = raw_dir / "nested" / ".." / raw_path.name
    resolved_path = raw_path.resolve()

    with (
        patch(
            "vector_lake.tool_delete.os.path.exists",
            return_value=True,
        ) as exists,
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        result = delete_source(str(unresolved_input), dry_run=False)

    assert str(resolved_path) in result
    assert [call.args[0] for call in exists.call_args_list] == [resolved_path]
    _assert_quarantine_removed(remove, raw_path)


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
    write_page(
        "Source_Multi-Legacy.md",
        "source",
        ["raw/foo.pdf", "raw/keep.pdf"],
    )
    write_page("Concept_Exact.md", "concept", ["raw/foo.pdf", "raw/keep.pdf"])
    write_page("Concept_Sole.md", "concept", ["raw/foo.pdf"])
    write_page("Source_foobar.md", "source", ["raw/foobar.pdf"])
    write_page("Concept_Substring.md", "concept", ["raw/archive/foo.pdf.notes"])

    preview = delete_source(str(raw_path), dry_run=True)

    assert "[WIKI] 4 affected wiki page(s)" in preview
    for expected in (
        "Source_Foo-Legacy.md",
        "Source_Multi-Legacy.md",
        "Concept_Exact.md",
        "Concept_Sole.md",
    ):
        assert expected in preview
    assert "Source_foo.md" not in preview
    assert "Source_foobar.md" not in preview
    assert "Concept_Substring.md" not in preview

    captured = []

    def capture_mutations(mutations, **kwargs):
        kwargs["precondition_callback"]()
        captured.extend(mutations)
        return {"deferred": []}

    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch",
            side_effect=capture_mutations,
        ),
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        applied = delete_source(str(raw_path), dry_run=False)

    by_filename = {item["filename"]: item for item in captured}
    assert set(by_filename) == {
        "Source_Foo-Legacy.md",
        "Source_Multi-Legacy.md",
        "Concept_Exact.md",
        "Concept_Sole.md",
    }
    assert by_filename["Source_Foo-Legacy.md"]["is_delete"] is True
    assert by_filename["Concept_Sole.md"]["is_delete"] is True
    assert "raw/foo.pdf" not in by_filename["Concept_Exact.md"]["content"]
    assert "raw/keep.pdf" in by_filename["Concept_Exact.md"]["content"]
    multi_source = by_filename["Source_Multi-Legacy.md"]
    assert multi_source.get("is_delete") is not True
    assert "raw/foo.pdf" not in multi_source["content"]
    assert "raw/keep.pdf" in multi_source["content"]
    assert "wiki_deleted=2" in applied
    assert "wiki_updated=2" in applied
    _assert_quarantine_removed(remove, raw_path)


def test_delete_blocks_invalid_utf8_without_rewriting_wiki_or_raw(isolated_memory):
    raw_path = isolated_memory / "raw" / "invalid-source.pdf"
    raw_path.write_bytes(b"raw-revision")
    wiki_path = isolated_memory / "wiki" / "Concept_Invalid.md"
    wiki_bytes = (
        b"---\n"
        b"id: Concept_Invalid\n"
        b"title: Invalid UTF-8\n"
        b"type: concept\n"
        b"sources: [raw/invalid-source.pdf, raw/keep.pdf]\n"
        b"---\n"
        b"body-before-"
        b"\xff"
        b"-body-after\n"
    )
    wiki_path.write_bytes(wiki_bytes)
    raw_bytes = raw_path.read_bytes()

    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch"
        ) as execute_mutations,
        patch("vector_lake.tool_delete.os.replace") as replace,
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        result = delete_source(str(raw_path), dry_run=False)

    assert "[BLOCKED]" in result
    assert "read strict UTF-8" in result
    assert "No changes made" in result
    execute_mutations.assert_not_called()
    replace.assert_not_called()
    remove.assert_not_called()
    assert wiki_path.read_bytes() == wiki_bytes
    assert raw_path.read_bytes() == raw_bytes


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
        assert (
            mcp_server.gc_vector_lake(
                days=7,
                dry_run=False,
                orphan_confirmation=confirmation,
            )
            == "applied"
        )
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

    def capture_mutations(mutations, **kwargs):
        kwargs["precondition_callback"]()
        captured.extend(mutations)
        return {"deferred": []}

    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch",
            side_effect=capture_mutations,
        ),
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        applied = delete_source(str(raw_path), dry_run=False)

    assert len(captured) == 1
    assert captured[0]["filename"] == "Source_foo.MD"
    assert captured[0]["is_delete"] is True
    assert len(captured[0]["expected_projection_hash"]) == 64
    assert "wiki_deleted=1" in applied
    _assert_quarantine_removed(remove, raw_path)


@pytest.mark.parametrize(
    ("sources", "expected_action"),
    [
        (["raw/foo.pdf"], "DELETE"),
        (["raw/foo.pdf", "raw/keep.pdf"], "REMOVE_REF"),
    ],
)
def test_delete_blocks_if_wiki_projection_changes_after_scan(
    isolated_memory,
    sources,
    expected_action,
):
    raw_path = isolated_memory / "raw" / "foo.pdf"
    raw_path.write_text("raw", encoding="utf-8")
    wiki_page = isolated_memory / "wiki" / "Concept_Race.md"
    rendered_sources = ", ".join(sources)
    wiki_page.write_text(
        "---\n"
        "id: concept_race\n"
        "title: Concept Race\n"
        "type: concept\n"
        f"sources: [{rendered_sources}]\n"
        "---\nold body\n",
        encoding="utf-8",
    )
    concurrent_content = (
        "---\n"
        "id: concept_race\n"
        "title: Concurrent Update\n"
        "type: concept\n"
        f"sources: [{rendered_sources}]\n"
        "---\nnew concurrent body\n"
    )
    captured = []

    def inject_projection_race(mutations, **kwargs):
        captured.extend(mutations)
        wiki_page.write_text(concurrent_content, encoding="utf-8")
        kwargs["precondition_callback"]()
        raise AssertionError("projection precondition did not detect the race")

    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch",
            side_effect=inject_projection_race,
        ),
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        result = delete_source(str(raw_path), dry_run=False)

    assert len(captured) == 1
    mutation = captured[0]
    assert len(mutation["expected_projection_hash"]) == 64
    if expected_action == "DELETE":
        assert mutation["is_delete"] is True
    else:
        assert mutation.get("is_delete") is not True
        assert "raw/foo.pdf" not in mutation["content"]
        assert "raw/keep.pdf" in mutation["content"]
    assert wiki_page.read_text(encoding="utf-8") == concurrent_content
    assert raw_path.read_text(encoding="utf-8") == "raw"
    assert "Wiki projection changed after delete scan: Concept_Race.md" in result
    assert "raw source was preserved" in result.lower()
    remove.assert_not_called()


@pytest.mark.parametrize(
    "initial_content",
    [None, "original revision"],
    ids=["missing_then_created", "revision_replaced"],
)
def test_delete_preserves_new_raw_revision_after_wiki_commit(
    isolated_memory,
    initial_content,
):
    raw_path = isolated_memory / "raw" / "foo.pdf"
    if initial_content is not None:
        raw_path.write_text(initial_content, encoding="utf-8")
    wiki_page = isolated_memory / "wiki" / "Concept_Raw-Race.md"
    wiki_page.write_text(
        "---\n"
        "id: concept_raw_race\n"
        "title: Concept Raw Race\n"
        "type: concept\n"
        "sources: [raw/foo.pdf]\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    replacement = "new revision created after wiki commit"

    def inject_raw_race(_mutations, **kwargs):
        kwargs["precondition_callback"]()
        wiki_page.unlink()
        raw_path.write_text(replacement, encoding="utf-8")
        return {"deferred": []}

    with (
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch",
            side_effect=inject_raw_race,
        ),
        patch("vector_lake.tool_delete.os.remove") as remove,
    ):
        result = delete_source(str(raw_path), dry_run=False)

    assert raw_path.read_text(encoding="utf-8") == replacement
    assert not wiki_page.exists()
    assert "RAW_SOURCE_CHANGED" in result
    assert "raw source was preserved" in result.lower()
    remove.assert_not_called()


def test_delete_quarantines_and_restores_revision_swapped_after_recheck(
    isolated_memory,
):
    from vector_lake import tool_delete

    raw_path = isolated_memory / "raw" / "foo.pdf"
    raw_path.write_text("original revision", encoding="utf-8")
    wiki_page = isolated_memory / "wiki" / "Concept_Final-Race.md"
    wiki_page.write_text(
        "---\n"
        "id: concept_final_race\n"
        "title: Concept Final Race\n"
        "type: concept\n"
        "sources: [raw/foo.pdf]\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    replacement = "replacement installed after final recheck"
    real_capture = tool_delete._capture_raw_revision
    raw_capture_count = 0

    def capture_with_final_swap(path):
        nonlocal raw_capture_count
        revision = real_capture(path)
        if Path(path) == raw_path:
            raw_capture_count += 1
            if raw_capture_count == 3:
                raw_path.write_text(replacement, encoding="utf-8")
        return revision

    def commit_wiki(_mutations, **kwargs):
        kwargs["precondition_callback"]()
        return {"deferred": []}

    with (
        patch(
            "vector_lake.tool_delete._capture_raw_revision",
            side_effect=capture_with_final_swap,
        ),
        patch(
            "vector_lake.mutation_coordinator.execute_mutation_batch",
            side_effect=commit_wiki,
        ),
    ):
        result = delete_source(str(raw_path), dry_run=False)

    assert raw_capture_count == 3
    assert raw_path.read_text(encoding="utf-8") == replacement
    quarantines = list(raw_path.parent.glob("*.quarantine"))
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8") == replacement
    assert "RAW_SOURCE_QUARANTINE_MISMATCH" in result
    assert f"preserved at {quarantines[0]}" in result
    assert "raw source was preserved" in result.lower()


def test_delete_restores_raw_and_retains_quarantine_when_delete_fails(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_delete

    raw_path = isolated_memory / "raw" / "unlink-failure.pdf"
    raw_path.write_text("original revision", encoding="utf-8")
    real_remove = tool_delete.os.remove
    quarantine_remove_attempts = 0

    def fail_first_quarantine_remove(path):
        nonlocal quarantine_remove_attempts
        candidate = Path(path)
        if candidate.name.endswith(".quarantine"):
            quarantine_remove_attempts += 1
            if quarantine_remove_attempts == 1:
                raise OSError("injected quarantine unlink failure")
        return real_remove(path)

    monkeypatch.setattr(tool_delete.os, "remove", fail_first_quarantine_remove)

    result = tool_delete.delete_source(str(raw_path), dry_run=False)

    assert quarantine_remove_attempts == 1
    assert raw_path.read_text(encoding="utf-8") == "original revision"
    quarantines = list(raw_path.parent.glob("*.quarantine"))
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8") == "original revision"
    assert "raw_deleted=False" in result
    assert "RAW_SOURCE_DELETE: injected quarantine unlink failure" in result
    assert f"preserved_at={quarantines[0]}" in result


def test_quarantine_recovery_retains_original_inode_when_raw_is_replaced(
    tmp_path,
    monkeypatch,
):
    from vector_lake import tool_delete

    quarantine_path = tmp_path / ".source.pdf.vector-lake-delete-race.quarantine"
    raw_path = tmp_path / "source.pdf"
    replacement_path = tmp_path / "replacement.pdf"
    quarantine_path.write_bytes(b"quarantined original")
    replacement_path.write_bytes(b"concurrent replacement")
    original_object = tool_delete._raw_object_key(
        tool_delete.os.stat(quarantine_path, follow_symlinks=False)
    )
    real_link = tool_delete.os.link

    def link_then_replace(source, destination, *, follow_symlinks=False):
        real_link(source, destination, follow_symlinks=follow_symlinks)
        tool_delete.os.replace(replacement_path, destination)

    monkeypatch.setattr(tool_delete.os, "link", link_then_replace)

    preserved_at = tool_delete._restore_quarantined_raw(
        quarantine_path,
        raw_path,
    )

    assert preserved_at == quarantine_path
    assert raw_path.read_bytes() == b"concurrent replacement"
    assert quarantine_path.read_bytes() == b"quarantined original"
    assert (
        tool_delete._raw_object_key(
            tool_delete.os.stat(quarantine_path, follow_symlinks=False)
        )
        == original_object
    )


def test_delete_blocks_when_any_markdown_frontmatter_is_damaged(
    isolated_memory,
):
    raw_path = isolated_memory / "raw" / "foo.pdf"
    raw_path.write_text("raw", encoding="utf-8")
    wiki_dir = isolated_memory / "wiki"
    (wiki_dir / "Source_foo.md").write_text(
        "---\nid: source_foo\ntype: source\nsources: [raw/foo.pdf]\n---\nbody\n",
        encoding="utf-8",
    )
    (wiki_dir / "Concept_Damaged.MD").write_text(
        "---\nid: broken\ncategories: [\n---\nbody\n",
        encoding="utf-8",
    )

    with (
        patch("vector_lake.mutation_coordinator.execute_mutation_batch") as execute,
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
