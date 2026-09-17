from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.schema_validator import validate_schema
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


def _dual_schema_page(entity_id, title, doc_type, *, aliases, moat, timeline, extra_h3=None):
    alias_block = "\n".join(f"- {alias}" for alias in aliases)
    extra = f"\n{extra_h3}\n\n- {title} 额外事实。\n" if extra_h3 else ""
    timeline_block = "\n".join(f"- {entry}" for entry in timeline)
    return f"""---
id: {entity_id}
title: {title}
type: {doc_type}
domain: Medical_IT
topic_cluster: General
status: Active
epistemic-status: seed
ttl: 1095
categories:
- System_Architecture
tags: []
created: '2026-01-01'
updated: '2026-01-02'
sources: []
aliases:
{alias_block}
---
## 1. 编译事实 (Compiled Truth - READ MODEL)

{title} 是主体。 (Last Reshaped: 2026-01-01)

### 核心护城河 (Moat)

- {moat} (Source: [[Source_S]])
{extra}
---

## 2. 证据时间线 (Timeline - EVENT STORE)

{timeline_block}
"""


def _section_aware_pair():
    left = _dual_schema_page(
        "vendor_a", "Vendor_A", "vendor", aliases=["Existing"],
        moat="[[Vendor_A]] 拥有护城河 A",
        timeline=["[2026-01-01] [Release] [[Vendor_A]] 发布 A。 (Source: [[Source_S]])"],
    )
    right = _dual_schema_page(
        "vendor_b", "Vendor_B", "vendor", aliases=["Alternate"],
        moat="[[Vendor_B]] 拥有护城河 B",
        extra_h3="### 关键产品线 (Key Products)",
        timeline=[
            "[2026-01-01] [Release] [[Vendor_A]] 发布 A。 (Source: [[Source_S]])",
            "[2026-01-02] [Observation] [[Vendor_B]] 观察到 B。 (Source: [[Source_S]])",
        ],
    )
    return left, right


def test_section_aware_merge_is_schema_valid():
    left, right = _section_aware_pair()

    merged = merge_markdown_content(left, right)
    frontmatter, body = split_frontmatter(merged)

    # The regression: the consumed page's compiled-truth bullets used to land after
    # `## 2. 证据时间线`, where the validator reads them as malformed events.
    validate_schema(frontmatter, body, "Vendor_A.md")
    assert body.count("## 2. 证据时间线") == 1
    assert body.count("## 1. 编译事实") == 1


def test_section_aware_merge_unions_both_sections():
    left, right = _section_aware_pair()

    merged = merge_markdown_content(left, right)
    _, body = split_frontmatter(merged)

    section_1, _, section_2 = body.partition("## 2. 证据时间线")
    # Both compiled-truth facts stay above the event store.
    assert "护城河 A" in section_1
    assert "护城河 B" in section_1
    assert "护城河 B" not in section_2
    # Timeline entries union and de-duplicate.
    assert section_2.count("发布 A。") == 1
    assert section_2.count("观察到 B。") == 1
    # One H3 slot, both bodies folded into it.
    assert body.count("### 核心护城河 (Moat)") == 1
    assert "### 关键产品线 (Key Products)" in body


def test_invalid_h3_for_surviving_type_is_demoted_not_dropped():
    left = _dual_schema_page(
        "vendor_a", "Vendor_A", "vendor", aliases=[],
        moat="[[Vendor_A]] 拥有护城河 A",
        timeline=["[2026-01-01] [Release] [[Vendor_A]] 发布 A。 (Source: [[Source_S]])"],
    )
    right = _dual_schema_page(
        "product_b", "Product_B", "product", aliases=[],
        moat="[[Product_B]] 拥有护城河 B",
        extra_h3="### 临床与管理价值流 (Clinical & Admin Value)",
        timeline=["[2026-01-02] [Observation] [[Product_B]] 观察到 B。 (Source: [[Source_S]])"],
    )

    merged = merge_markdown_content(left, right)
    frontmatter, body = split_frontmatter(merged)

    validate_schema(frontmatter, body, "Vendor_A.md")
    # A product-only slot is invalid on the surviving vendor page: keep the facts,
    # drop the heading.
    assert "### 临床与管理价值流" not in body
    assert "临床与管理价值流" in body
    assert "额外事实" in body


def test_section_aware_merge_stays_pure():
    left, right = _section_aware_pair()
    left_before, right_before = left, right

    merge_markdown_content(left, right)

    assert left == left_before
    assert right == right_before
