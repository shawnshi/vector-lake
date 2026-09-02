from collections import defaultdict

from vector_lake.tool_lint import (
    _BoundedIssueCollector,
    _lint_record,
    _register_link_target,
    _resolve_link_target,
    lint_vector_lake,
)
from vector_lake.wiki_utils import iter_wiki_link_matches, markdown_fenced_code_spans


def test_temporal_link_is_not_a_missing_page_target():
    assert _resolve_link_target("2026-07-13", {}, defaultdict(set)) is None


def test_link_prefix_variation_resolves_only_unique_target():
    exact = {}
    normalized = defaultdict(set)
    _register_link_target(exact, normalized, "Institution_梅奥诊所", "Institution_梅奥诊所")

    assert (
        _resolve_link_target("Vendor_梅奥诊所", exact, normalized)
        == "Institution_梅奥诊所"
    )


def test_ambiguous_normalized_link_remains_unresolved():
    exact = {}
    normalized = defaultdict(set)
    _register_link_target(exact, normalized, "Concept_Meta", "Concept_Meta")
    _register_link_target(exact, normalized, "Vendor_Meta", "Vendor_Meta")

    assert _resolve_link_target("Meta", exact, normalized) is None


def test_canonical_key_outranks_alias_registered_after_it():
    exact = {}
    normalized = defaultdict(set)
    _register_link_target(
        exact,
        normalized,
        "Concept_TEFCA",
        "Concept_TEFCA",
        canonical=True,
    )
    _register_link_target(
        exact,
        normalized,
        "Concept_TEFCA",
        "Policy_TEFCA-可信交换框架",
    )

    assert _resolve_link_target("Concept_TEFCA", exact, normalized) == "Concept_TEFCA"


def test_canonical_key_outranks_alias_registered_before_it():
    exact = {}
    normalized = defaultdict(set)
    _register_link_target(
        exact,
        normalized,
        "Concept_TEFCA",
        "Policy_TEFCA-可信交换框架",
    )
    _register_link_target(
        exact,
        normalized,
        "Concept_TEFCA",
        "Concept_TEFCA",
        canonical=True,
    )

    assert _resolve_link_target("Concept_TEFCA", exact, normalized) == "Concept_TEFCA"


def test_link_iterator_excludes_fenced_and_inline_code():
    content = """[[Visible]]

`[[Inline-Code]]`

```markdown
[[Fenced-Code]]
> ```text
> [[Nested-Literal]]
> ```
```
"""

    targets = [match.group(1) for match in iter_wiki_link_matches(content)]

    assert targets == ["Visible"]
    spans = markdown_fenced_code_spans(content)
    assert len(spans) == 1
    assert "[[Fenced-Code]]" in content[spans[0][0] : spans[0][1]]


def test_lint_issue_collector_retains_ten_samples_and_exact_count():
    collector = _BoundedIssueCollector(sample_limit=10)

    for index in range(100_000):
        collector.append(f"issue-{index}", category=f"kind-{index % 4}")

    assert len(collector) == 100_000
    assert collector[:10] == [f"issue-{index}" for index in range(10)]
    assert collector.retained_sample_count == 10
    assert collector.category_counts == {
        "kind-0": 25_000,
        "kind-1": 25_000,
        "kind-2": 25_000,
        "kind-3": 25_000,
    }
    snapshot = collector.category_counts
    snapshot["kind-0"] = 0
    assert collector.category_counts["kind-0"] == 25_000


def test_lint_similarity_report_has_bounded_breakdown_and_suppression(
    isolated_memory,
):
    wiki_dir = isolated_memory / "wiki"
    page_specs = [
        ("System_Community-L1-aaaaaaaa", "system", []),
        ("System_Community-L1-bbbbbbbb", "system", []),
        ("Source_Intelligence-20260301-Briefing", "source", ["raw/news/day-1.md"]),
        ("Source_Intelligence-20260302-Briefing", "source", ["raw/news/day-2.md"]),
        ("Concept_A_B", "concept", []),
        ("Concept_AB", "concept", []),
        ("Source_Report", "source", ["raw/reports/base.md"]),
        ("Source_Report-ab12cd34", "source", ["raw/reports/hash.md"]),
        ("Source_Raw-Left", "source", ["raw/shared.md"]),
        ("Source_Unrelated-Right", "source", ["raw/shared.md"]),
        ("Concept_Human-in-the-Loop", "concept", []),
        ("Concept_Human-on-the-Loop", "concept", []),
    ]
    for index, (page_key, entity_type, sources) in enumerate(page_specs):
        source_lines = "\n".join(f"- '{source}'" for source in sources) or "[]"
        (wiki_dir / f"{page_key}.md").write_text(
            f"""---
title: '{page_key}'
type: {entity_type}
domain: General
status: Active
epistemic-status: seed
categories:
- Uncategorized
sources: {source_lines if source_lines == '[]' else ''}
{'' if source_lines == '[]' else source_lines}
id: 20260811_similarity_{index:02d}
updated: '2026-08-11'
strategic_scope: core
---
# {page_key}

Candidate inventory fixture.
""",
            encoding="utf-8",
        )

    report = lint_vector_lake(auto_fix=False)

    assert "9. Filename Similarity: [INFO: 4]" in report
    assert (
        "Breakdown: exact=1 | hash_revision=1 | ambiguous_revision=0 | "
        "fuzzy=1 | raw_source=1"
        in report
    )
    assert (
        "Suppressed: temporal_series=1 | numeric_identity_conflict=0 | "
        "system_pages=2"
        in report
    )


def test_read_only_lint_record_does_not_retain_body():
    record = _lint_record(
        {"title": "Example"},
        "A long body that should not remain in the parsed record.",
        {"Concept_Target"},
        "Concept_Example.md",
        retain_body=False,
    )

    assert record["body"] is None
    assert record["body_length"] == 56


def test_lint_requests_non_materializing_governance_metrics(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import governance_metrics

    calls = []
    monkeypatch.setattr(
        governance_metrics,
        "compute_debt_metrics",
        lambda *, skip_heavy=False, read_only=False: calls.append(
            (skip_heavy, read_only)
        ) or {
            "unmanaged_unsupported_claim_count": 0,
            "managed_unsupported_claim_count": 0,
            "unmanaged_missing_link_target_count": 0,
            "managed_missing_link_target_count": 0,
            "stale_claim_count": 0,
            "pending_change_set_count": 0,
        },
    )

    lint_vector_lake(auto_fix=False)

    assert calls == [(True, True)]


def test_lint_without_auto_fix_preserves_database_file_identity(
    isolated_memory,
):
    from vector_lake import db_store

    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()

    def durable_identity():
        paths = (db_path, db_path.with_name(db_path.name + "-wal"))
        result = []
        for path in paths:
            try:
                stat = path.stat()
                result.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                result.append((path.name, "missing"))
        return tuple(result)

    before = durable_identity()
    lint_vector_lake(auto_fix=False)
    after = durable_identity()

    assert after == before
