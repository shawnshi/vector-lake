"""Section 17: a declared ``sources:`` path that no longer names a file is reported, never repaired.

The frontmatter declaration and the SQLite ``sources`` row agree with each other -- ``source_id``
is ``_stable_id("source", raw_ref)``, a hash of the path string -- so a repair that rewrote the
page alone would leave the file naming one path and the database another for the same source.
These tests pin that: the finding is reported with where the file went, and nothing is written.
"""

import json

from vector_lake.tool_lint import lint_vector_lake
from vector_lake.wiki_utils import get_raw_dir, get_wiki_dir

_BODY = (
    "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n- x\n\n"
    "## 2. 证据时间线 (Timeline - EVENT STORE)\n\n- [2026-09-23] [Observation] x\n"
)


def _page(name: str, sources: list[str]) -> None:
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / name).write_text(
        "---\n"
        f"id: test_{name[:-3].lower()}\n"
        f"title: {name[:-3]}\n"
        "type: concept\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories: [Healthcare_IT]\n"
        "updated: 2026-09-17\n"
        f"sources: {json.dumps(sources, ensure_ascii=False)}\n"
        "strategic_scope: core\n"
        "evidence_tier: derived\n"
        "---\n\n" + _BODY,
        encoding="utf-8",
    )


def _raw(relative: str) -> None:
    path = get_raw_dir() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("raw body\n", encoding="utf-8")


def _section(report: str, number: int) -> str:
    lines = report.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{number}. "))
    collected = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        collected.append(line)
    return "\n".join(collected)


def test_a_declared_raw_path_that_exists_passes(isolated_memory):
    _raw("notes/thing.md")
    _page("Concept_Existing-Source.md", ["raw/notes/thing.md"])

    section = _section(lint_vector_lake(), 17)

    assert "[PASS]" in section, section
    assert "1 declared raw path(s): 1 resolve" in section


def test_a_moved_raw_file_is_reported_with_where_it_went(isolated_memory):
    _raw("news/2026Q3/intelligence_20260704_briefing.md")
    _page("Concept_Moved-Source.md", ["raw/news/intelligence_20260704_briefing.md"])

    section = _section(lint_vector_lake(), 17)

    assert "[FAIL: 1]" in section, section
    assert "raw/news/intelligence_20260704_briefing.md -> raw/news/2026Q3/intelligence_20260704_briefing.md (moved)" in section
    assert "raw/news -> raw/news/2026Q3" in section


def test_the_check_writes_nothing(isolated_memory):
    """A repair here would desynchronise the page from the ``sources`` row it agrees with."""
    _raw("news/2026Q3/briefing.md")
    _page("Concept_Report-Only.md", ["raw/news/briefing.md"])
    page = get_wiki_dir() / "Concept_Report-Only.md"
    before = page.read_text(encoding="utf-8")
    raw_path = get_raw_dir() / "news/2026Q3/briefing.md"
    raw_before = raw_path.read_text(encoding="utf-8")

    report = lint_vector_lake()

    assert "[FAIL: 1]" in _section(report, 17)
    assert page.read_text(encoding="utf-8") == before
    assert raw_path.read_text(encoding="utf-8") == raw_before
    assert "Auto-fixed: 0" in report


def test_a_corrupted_path_and_a_missing_name_are_told_apart(isolated_memory):
    _page("Concept_Corrupted-Source.md", ["raw/youtube/???????:??AI-2024-05-23.md"])
    _page("Concept_Vanished-Source.md", ["raw/gone/nothing-here.md"])

    section = _section(lint_vector_lake(), 17)

    assert "[FAIL: 2]" in section, section
    assert "the stored path is corrupted: raw/youtube/???????:??AI-2024-05-23.md" in section
    assert "no file named nothing-here.md under raw/: raw/gone/nothing-here.md" in section
    # Two different owners: a mangled string is a write-path defect, a missing file is not.
    assert "moved" not in section
