from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import split_frontmatter


def _content(
    entity_id: str,
    title: str,
    body: str,
    aliases=None,
    extra_frontmatter: str = "",
) -> str:
    alias_line = f"aliases: {aliases}\n" if aliases else ""
    return f"""---
id: {entity_id}
title: {title}
type: source
status: Active
{alias_line}{extra_frontmatter}---
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


def test_semantic_merge_unions_source_metadata_and_omits_backlog_raw_preview():
    left = _content(
        "source_curated",
        "Curated",
        "Curated analysis.",
        aliases="[Existing]",
        extra_frontmatter=(
            "topic_cluster: Multi-Agent\n"
            "categories: [Artificial_Intelligence]\n"
            "tags: [curated]\n"
            "sources: ['[[Source_Auto_Fixed]]']\n"
            "relations:\n"
            "  - predicate: supports\n"
            "    target: Concept_Curated\n"
        ),
    )
    right = _content(
        "source_backlog",
        "2605.14892v2",
        "Raw Preview MUST NOT SURVIVE.\n\n```text\nraw bytes\n```",
        aliases="[Historical]",
        extra_frontmatter=(
            "topic_cluster: Raw_Ingest_Backlog\n"
            "categories: [Raw_Ingest_Backlog]\n"
            "tags: [backlog]\n"
            "sources: [raw/Huggingface-Daily-Papers/2605.14892v2.md]\n"
            "relations:\n"
            "  - predicate: derived-from\n"
            "    target: Concept_Raw\n"
            "source_artifacts:\n"
            "  raw/Huggingface-Daily-Papers/2605.14892v2.md:\n"
            "    classification: research\n"
        ),
    )

    merged = merge_markdown_content(
        left,
        right,
        source_key="Source_2605-14892v2-0dadf455",
    )
    frontmatter, body = split_frontmatter(merged)

    assert frontmatter["id"] == "source_curated"
    assert frontmatter["sources"] == ["raw/Huggingface-Daily-Papers/2605.14892v2.md"]
    assert frontmatter["categories"] == ["Artificial_Intelligence"]
    assert frontmatter["tags"] == ["curated", "backlog"]
    assert frontmatter["relations"] == [
        {"predicate": "supports", "target": "Concept_Curated"},
    ]
    assert frontmatter["source_artifacts"] == {
        "raw/Huggingface-Daily-Papers/2605.14892v2.md": {
            "classification": "research"
        }
    }
    assert "Source_2605-14892v2-0dadf455" in frontmatter["aliases"]
    assert "Raw Preview MUST NOT SURVIVE" not in body
    assert body.strip() == "Curated analysis."
