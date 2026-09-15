"""Canonical Wiki page write path.

Validation belongs above the base layer, and the commit belongs above storage.
``wiki_utils.atomic_write_text`` used to do both -- it validated canonical Markdown
and ``write_markdown_file`` committed through the mutation coordinator -- which put
schema validation and a canonical mutation inside the base layer and made the base
module import two higher layers.

Splitting it is all-or-nothing for the dependency cycle: removing any one or two of
wiki_utils' three upward edges leaves the largest strongly connected component
unchanged at 40, and only removing all three takes it to 32.

  atomic_write_text            base, plain atomic write + compare-and-swap
  write_canonical_markdown     validates, then delegates to atomic_write_text
  write_markdown_file          builds page content and commits a mutation
"""

from __future__ import annotations

from pathlib import Path

from vector_lake import defense_hook, schema_validator
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.yaml_utils import dump_yaml
from vector_lake.wiki_utils import (
    SafeWriteError,
    atomic_write_text,
    count_list_items,
    get_index_path,
    get_wiki_dir,
    is_canonical_wiki_markdown_path,
    read_markdown_file,
    reject_ambiguous_windows_path,
    split_frontmatter,
    validate_wiki_filename,
)


def validate_canonical_markdown(
    path: str | Path,
    content: str,
    *,
    pre_parsed_frontmatter: dict | None = None,
    validation_mode: str = "full",
) -> None:
    """Validate canonical Wiki Markdown before it is written.

    Only canonical Markdown under the wiki root is validated; callers writing JSON
    receipts are unaffected because the path check rejects them. An invalid
    ``validation_mode`` is rejected, exactly as the old inline copy did.
    """
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    target = Path(path)
    # Reject an ambiguous Windows alias first: that is the order the old inline copy
    # used, and validating first would report a schema error for a path problem.
    reject_ambiguous_windows_path(target)
    if not is_canonical_wiki_markdown_path(target):
        return
    parsed_frontmatter, _ = split_frontmatter(content)
    if (
        pre_parsed_frontmatter is not None
        and pre_parsed_frontmatter != parsed_frontmatter
    ):
        raise ValueError("Pre-parsed frontmatter does not match content")
    if validation_mode == "full":
        defense_hook.verify_asset(
            content, target.name, parsed_frontmatter, get_index_path()
        )
    else:
        schema_validator.validate_schema(parsed_frontmatter, content, target.name)


def write_canonical_markdown(
    path: str | Path,
    content: str,
    *,
    pre_parsed_frontmatter: dict | None = None,
    validation_mode: str = "full",
    expected_current_hash: str | None = None,
) -> None:
    """Validate a canonical page and then write it, in that order."""
    validate_canonical_markdown(
        path,
        content,
        pre_parsed_frontmatter=pre_parsed_frontmatter,
        validation_mode=validation_mode,
    )
    atomic_write_text(path, content, expected_current_hash=expected_current_hash)


def write_markdown_file(
    path: str | Path,
    frontmatter: dict,
    body: str,
    skip_validation: bool = False,
):
    """Build page content from frontmatter and body, then commit it.

    Moved here from wiki_utils: committing a canonical mutation is orchestration,
    not a base-layer utility. Behaviour is unchanged.
    """
    path = Path(path)
    # Check traversal
    try:
        if path.resolve().is_relative_to(get_wiki_dir().resolve()) is False and "MEMORY" not in str(path):
            raise SafeWriteError(f"Path traversal blocked: {path}")
    except Exception:
        pass
    if not skip_validation and path.exists():
        try:
            _, old_body, _ = read_markdown_file(path)
            old_truth_count = count_list_items(old_body, "编译事实") or count_list_items(old_body, "Compiled Truth")
            new_truth_count = count_list_items(body, "编译事实") or count_list_items(body, "Compiled Truth")
            if new_truth_count < old_truth_count:
                raise SafeWriteError(f"丢失了编译事实 (Compiled Truth)。旧文件有 {old_truth_count} 条，新文件只有 {new_truth_count} 条。请调用 read_resource 重新读取当前文件状态，并使用 Append 模式进行增量合并，而不是直接覆盖。")
            old_timeline_count = count_list_items(old_body, "证据时间线") or count_list_items(old_body, "Evidence Timeline")
            new_timeline_count = count_list_items(body, "证据时间线") or count_list_items(body, "Evidence Timeline")
            if new_timeline_count < old_timeline_count:
                raise SafeWriteError(f"丢失了证据时间线 (Evidence Timeline)。旧文件有 {old_timeline_count} 条记录，新文件只有 {new_timeline_count} 条记录。请调用 read_resource 重新读取当前文件状态，并使用 Append 模式进行增量合并，而不是直接覆盖。")
        except SafeWriteError:
            raise
        except Exception:
            pass

    filename = path.name
    if not skip_validation:
        validate_wiki_filename(filename)

    if filename.startswith("Synthesis_STORM_") and not skip_validation:
        required_headers = [
            "## 1. Top 5 Key Findings",
            "## 2. The Contradiction Map",
            "## 3. Actionable Insights",
            "## 4. Multi-Perspective Raw Scan",
            "## 5. Peer Review"
        ]
        for header in required_headers:
            if header not in body:
                raise SafeWriteError(f"STORM Synthesis Structural Violation: The file {filename} is missing mandatory H2 section '{header}'. Please strictly follow the references/storm_report_template.md structure.")
    yaml_block = dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
    full_content = f"---\n{yaml_block}---\n{body.lstrip()}"
    expected_path = (get_wiki_dir() / filename).resolve()
    if path.resolve() != expected_path:
        raise SafeWriteError(f"Path traversal blocked: {path}")
    execute_mutation_plan(filename, content=full_content, is_delete=False)


def safe_write_markdown(
    path: str | Path,
    content: str,
    skip_validation: bool = False,
):
    """Split content into frontmatter and body, then commit it as a page."""
    frontmatter, body = split_frontmatter(content)
    write_markdown_file(path, frontmatter, body, skip_validation=skip_validation)
