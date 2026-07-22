import re
from pathlib import Path

from vector_lake.schema_validator import VALID_STATUS
from vector_lake.tool_lint import lint_vector_lake


def _source_page(status: str, suffix: str) -> str:
    return f"""---
id: status_{suffix.lower()}
title: Status {suffix}
type: source
domain: General
status: {status}
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-07-23
sources: []
---
Status vocabulary contract test.
"""


def test_schema_document_status_vocabulary_matches_validator():
    schema_text = (Path(__file__).parents[1] / "schema.md").read_text(encoding="utf-8")
    match = re.search(r'^\s*status:\s*"([^"]+)"', schema_text, flags=re.MULTILINE)

    assert match is not None
    documented_statuses = {item.strip() for item in match.group(1).split("|")}
    assert documented_statuses == VALID_STATUS


def test_lint_accepts_every_schema_status(isolated_memory):
    wiki_dir = isolated_memory / "wiki"
    for status in sorted(VALID_STATUS):
        (wiki_dir / f"Source_Status-{status}.md").write_text(
            _source_page(status, status),
            encoding="utf-8",
        )

    report = lint_vector_lake(auto_fix=False)

    assert "Invalid status" not in report


def test_lint_still_rejects_status_outside_schema(isolated_memory):
    wiki_dir = isolated_memory / "wiki"
    (wiki_dir / "Source_Status-Unknown.md").write_text(
        _source_page("Unknown", "Unknown"),
        encoding="utf-8",
    )

    report = lint_vector_lake(auto_fix=False)

    assert "Invalid status 'unknown'" in report
