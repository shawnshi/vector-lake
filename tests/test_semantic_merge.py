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


# --- Evidence metadata is a union, not an inheritance -------------------------
#
# Every case below is a regression on one behaviour of the old merge: it inherited
# the survivor's frontmatter apart from ``aliases`` and dropped the left body's
# pre-``## 1.`` prefix.  Both deleted evidence while the merge reported success.


def _metadata_page(
    entity_id,
    title,
    *,
    sources="[]",
    tags="[]",
    categories=None,
    extra_frontmatter="",
    updated="2026-01-02",
    h1=None,
):
    category_block = categories or ["System_Architecture"]
    rendered_categories = "\n".join(f"- {value}" for value in category_block)
    return f"""---
id: {entity_id}
title: {title}
type: vendor
domain: Medical_IT
topic_cluster: General
status: Active
epistemic-status: seed
ttl: 1095
categories:
{rendered_categories}
tags: {tags}
created: '2026-01-01'
updated: '{updated}'
sources: {sources}
aliases: []
{extra_frontmatter}---
{h1 or ''}## 1. 编译事实 (Compiled Truth - READ MODEL)

{title} 是主体。 (Last Reshaped: 2026-01-01)

### 核心护城河 (Moat)

- {title} 拥有护城河。 (Source: [[Source_S]])

---

## 2. 证据时间线 (Timeline - EVENT STORE)

- [2026-01-01] [Release] {title} 发布。 (Source: [[Source_S]])
"""


def test_merge_unions_evidence_metadata_and_keeps_survivor_identity():
    left = _metadata_page("vendor_a", "Vendor_A", sources="[raw/left.md]", tags="['#left']",
                          categories=["System_Architecture"])
    right = _metadata_page("vendor_b", "Vendor_B", sources="[raw/right.md]", tags="['#right']",
                           categories=["Healthcare_IT"],
                           extra_frontmatter="evidence_tier: commercial-commitment\n")

    merged = merge_markdown_content(left, right)
    frontmatter, body = split_frontmatter(merged)

    # Identity stays the survivor's: copying the consumed page's id or title would
    # re-label this page as the thing that was merged into it.
    assert frontmatter["id"] == "vendor_a"
    assert frontmatter["title"] == "Vendor_A"
    assert frontmatter["created"] == "2026-01-01"

    # The consumed page's evidence metadata survives the deletion of its file.
    assert frontmatter["sources"] == ["raw/left.md", "raw/right.md"]
    assert frontmatter["tags"] == ["#left", "#right"]
    assert frontmatter["evidence_tier"] == "commercial-commitment"

    validate_schema(frontmatter, body, "Vendor_A.md")


def test_merge_does_not_union_the_single_valued_category():
    """``categories`` looks additive because it is a list; it is not.

    The purpose contract requires exactly one domain, so unioning two pages' categories
    produces a page the write gate then refuses.  This test previously asserted the union,
    which is how the defect shipped: it encoded the wrong contract and nothing ran the
    merged frontmatter through the gate.
    """
    left = _metadata_page("vendor_a", "Vendor_A", categories=["System_Architecture"])
    right = _metadata_page("vendor_b", "Vendor_B", categories=["Healthcare_IT"])

    frontmatter, body = split_frontmatter(merge_markdown_content(left, right))

    assert frontmatter["categories"] == ["System_Architecture"]
    assert len(frontmatter["categories"]) == 1
    validate_schema(frontmatter, body, "Vendor_A.md")


def test_a_merge_between_different_domains_stays_writable():
    """The failure that exposed this: the merge produced two domains and was rejected.

    Both halves matter.  The gate refusing is correct, but a merge that cannot commit
    leaves the pair permanently unmergeable, so the merge must simply not produce it.
    """
    left = _metadata_page("vendor_a", "Vendor_A", categories=["Policy_and_Governance"],
                          tags="['#a']", sources="[raw/a.md]")
    right = _metadata_page("vendor_b", "Vendor_B", categories=["System_Architecture"],
                           tags="['#b']", sources="[raw/b.md]")

    frontmatter, body = split_frontmatter(merge_markdown_content(left, right))

    assert frontmatter["categories"] == ["Policy_and_Governance"]
    validate_schema(frontmatter, body, "Vendor_A.md")


def test_merge_keeps_the_survivor_h1():
    left = _metadata_page("vendor_a", "Vendor_A", h1="# [[Vendor_A|Vendor_A]]\n")
    right = _metadata_page("vendor_b", "Vendor_B", h1="# [[Vendor_B|Vendor_B]]\n")

    _, body = split_frontmatter(merge_markdown_content(left, right))

    # The prefix used to be sliced off with the leading heading, so the merged page
    # lost a title line both inputs carried.
    assert body.startswith("# [[Vendor_A|Vendor_A]]")
    assert body.count("# [[Vendor_A|Vendor_A]]") == 1
    assert "Vendor_B|Vendor_B" not in body
    assert body.count("## 1. 编译事实") == 1


def test_merge_tag_union_respects_the_taxonomy_limit(caplog):
    import logging

    from vector_lake.schema_validator import MAX_TAGS

    left = _metadata_page("vendor_a", "Vendor_A", tags="['#a', '#b', '#c']")
    right = _metadata_page("vendor_b", "Vendor_B", tags="['#d']")

    with caplog.at_level(logging.WARNING, logger="vector-lake-semantic-merge"):
        merged = merge_markdown_content(left, right)
    frontmatter, body = split_frontmatter(merged)

    # Truncated to the bound the write gate enforces, survivor's tags first...
    assert frontmatter["tags"] == ["#a", "#b", "#c"]
    assert len(frontmatter["tags"]) == MAX_TAGS
    # ...and the drop is disclosed rather than silent.
    assert any("#d" in record.getMessage() for record in caplog.records)
    validate_schema(frontmatter, body, "Vendor_A.md")


def test_merge_does_not_move_updated_backwards():
    left = _metadata_page("vendor_a", "Vendor_A", updated="2026-09-12")
    right = _metadata_page("vendor_b", "Vendor_B", updated="2026-09-07T08:40:25.890500+00:00")
    # The store holds both a bare date and a full timestamp; a lexical max over the raw
    # strings would let the longer form of an earlier day win.
    assert split_frontmatter(merge_markdown_content(left, right))[0]["updated"] == "2026-09-12"


def test_same_day_updated_keeps_the_survivor_stamp():
    # The comparison is on the date part, so a same-day pairing is a tie and a tie
    # keeps the survivor's own stamp: a merge is not a reason to re-time a page.
    left = _metadata_page("vendor_a", "Vendor_A", updated="2026-09-12")
    newer_right = _metadata_page("vendor_b", "Vendor_B", updated="2026-09-12T06:00:00+00:00")
    assert split_frontmatter(merge_markdown_content(left, newer_right))[0]["updated"] == "2026-09-12"

    later_right = _metadata_page("vendor_b", "Vendor_B", updated="2026-09-13T06:00:00+00:00")
    assert (
        split_frontmatter(merge_markdown_content(left, later_right))[0]["updated"]
        == "2026-09-13T06:00:00+00:00"
    )


# --- The consumed page's filename dies with the file --------------------------------


def test_the_consumed_page_key_survives_as_an_alias():
    """``[[ConsumedKey]]`` is how the rest of the wiki addresses the page being merged away.

    The key lives in the filename, not in the frontmatter, so it cannot be recovered from
    ``right_content`` -- hence the explicit argument.  Without it, 321 links across 33 of
    the 3793 merges on record went dangling and had to be repaired by hand.
    """
    left = _metadata_page("vendor_a", "Vendor_A")
    right = _metadata_page("vendor_b", "Vendor_B")

    merged = merge_markdown_content(left, right, consumed_page_key="Vendor_B-Page")
    frontmatter, _ = split_frontmatter(merged)

    assert "Vendor_B-Page" in frontmatter["aliases"]
    # The title still joins as before.
    assert "Vendor_B" in frontmatter["aliases"]


def test_a_key_already_covered_by_the_title_is_not_duplicated():
    # Common case: the consumed page's filename and title are the same string.
    left = _metadata_page("vendor_a", "Vendor_A")
    right = _metadata_page("vendor_b", "Vendor_B")

    frontmatter, _ = split_frontmatter(
        merge_markdown_content(left, right, consumed_page_key="Vendor_B")
    )

    assert frontmatter["aliases"].count("Vendor_B") == 1


def test_omitting_the_key_keeps_the_previous_behaviour():
    # Callers that cannot supply a key must not get a different alias set.
    left = _metadata_page("vendor_a", "Vendor_A")
    right = _metadata_page("vendor_b", "Vendor_B")

    frontmatter, _ = split_frontmatter(merge_markdown_content(left, right))

    assert "Vendor_B" in frontmatter["aliases"]
    assert not any("Page" in a for a in frontmatter["aliases"])
