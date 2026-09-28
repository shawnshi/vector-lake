"""Section 1: a page carrying two frontmatter blocks is reported.

Measured 2026-09-28: **41 live pages** were written twice, so a placeholder block -- ``title`` equal
to the filename, ``categories: [Uncategorized]``, ``sources: []`` -- sits above the page's real
block, which then became body text.  The page is classified by the placeholder while its real title,
domain, category and sources are unreachable.  All 41 carry ``Uncategorized`` in the first block,
35 have a filename for a title, and **39 declare their sources in the second block** -- so part of
the "unsourced claims" debt was this shape rather than missing provenance.

Nothing is repaired here: which block is authoritative is a judgement about two candidate records,
and rewriting the wrong one would discard the fields the page is currently classified by.
"""

from vector_lake.tool_lint import lint_vector_lake
from vector_lake.wiki_utils import get_wiki_dir

_BODY = (
    "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n- x\n\n"
    "## 2. 证据时间线 (Timeline - EVENT STORE)\n\n- [2026-09-23] [Observation] x\n"
)


def _section(report: str, number: int) -> str:
    lines = report.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{number}. "))
    collected = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("    "):
            break
        collected.append(line)
    return "\n".join(collected)


def _write(name: str, text: str) -> None:
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / name).write_text(text, encoding="utf-8")


def _frontmatter(title: str, category: str, sources: str) -> str:
    return (
        "---\n"
        f"id: test_{title}\n"
        f"title: {title}\n"
        "type: concept\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        f"categories: [{category}]\n"
        "updated: 2026-09-17\n"
        f"{sources}"
        "strategic_scope: core\n"
        "---\n"
    )


def test_a_single_frontmatter_block_passes(isolated_memory):
    _write("Concept_One-Block.md", _frontmatter("One Block", "Healthcare_IT", "sources: []\n") + "\n" + _BODY)

    section = _section(lint_vector_lake(), 1)

    assert "[PASS]" in section, section


def test_a_second_frontmatter_block_is_reported(isolated_memory):
    doubled = (
        _frontmatter("Concept_Two-Blocks", "Uncategorized", "sources: []\n")
        + "---\n"
        "id: real\n"
        "title: Two Blocks\n"
        "type: concept\n"
        "sources: ['raw/notes/thing.md']\n"
        "---\n"
        + _BODY
    )
    _write("Concept_Two-Blocks.md", doubled)

    section = _section(lint_vector_lake(), 1)

    assert "[FAIL: 1]" in section, section
    assert "Concept_Two-Blocks.md: contains a second YAML frontmatter block in the body" in section


def test_the_check_writes_nothing(isolated_memory):
    """The lint must not pick a winner: one block or the other is the page's real record."""
    doubled = (
        _frontmatter("Concept_Hands-Off", "Uncategorized", "sources: []\n")
        + "---\n"
        "id: real\n"
        "title: Hands Off\n"
        "---\n"
        + _BODY
    )
    _write("Concept_Hands-Off.md", doubled)
    before = (get_wiki_dir() / "Concept_Hands-Off.md").read_text(encoding="utf-8")

    lint_vector_lake()

    after = (get_wiki_dir() / "Concept_Hands-Off.md").read_text(encoding="utf-8")
    assert after == before
