from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import split_frontmatter


def _content(entity_id: str, title: str, body: str, aliases=None) -> str:
    alias_line = f"aliases: {aliases}\n" if aliases else ""
    return f"""---
id: {entity_id}
title: {title}
type: source
status: Active
{alias_line}---
{body}
"""


def test_semantic_merge_is_pure_and_preserves_source_aliases():
    left = _content("source_left", "Left", "Left body.", "[Existing]")
    right = _content("source_right", "Right", "Right body.", "[Alternate]")

    merged = merge_markdown_content(left, right)
    frontmatter, body = split_frontmatter(merged)

    assert left.endswith("Left body.\n")
    assert right.endswith("Right body.\n")
    assert frontmatter["id"] == "source_left"
    assert frontmatter["aliases"] == ["Existing", "Alternate", "Right"]
    assert "Left body." in body
    assert "## Merged from Right" in body
    assert "Right body." in body


def test_semantic_merge_inserts_source_before_timeline_and_demotes_headings():
    left = _content(
        "source_left",
        "Left",
        "## 1. 编译事实\n\nLeft facts.\n\n## 2. 证据时间线\n\n- [2026-07-16] [Observation] Left event.",
    )
    right = _content(
        "source_right",
        "Right",
        "# Right title\n\n## 1. 编译事实\n\n- Right fact.",
    )

    merged = merge_markdown_content(left, right, source_key="Source_Right")
    frontmatter, body = split_frontmatter(merged)

    assert frontmatter["aliases"] == ["Source_Right", "Right"]
    assert body.index("## Merged from Right") < body.index("## 2. 证据时间线")
    assert "**Right title**" in body
    assert "**1. 编译事实**" in body
