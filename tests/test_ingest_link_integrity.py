import hashlib
import json

import pytest

from vector_lake import db_store, governance_store, tool_ingest
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.schema_validator import SchemaViolationException, validate_schema


def _source_frontmatter(categories=None):
    return {
        "id": "20260809_test",
        "title": "Source Test",
        "type": "source",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": categories or ["Uncategorized"],
        "updated": "2026-08-09",
        "sources": ["raw/test.md"],
    }


def _bare_content(body, *, title, node_type, aliases=None):
    lines = ["---", f"title: {title}", f"type: {node_type}"]
    if aliases:
        lines.append("aliases:")
        lines.extend(f"- {alias}" for alias in aliases)
    lines.extend(["---", "", body, ""])
    return "\n".join(lines)


def _valid_source_content(body, *, entity_id="source_test", title="Source Test"):
    return f"""---
id: {entity_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-08-09
sources: [raw/test.md]
---
{body}
"""


def _valid_concept_content(
    key="Concept_Target",
    *,
    title="Target Concept",
    aliases=None,
    extra_body="",
):
    alias_lines = "\n".join(f"- {alias}" for alias in (aliases or []))
    aliases_yaml = f"aliases:\n{alias_lines}\n" if aliases else ""
    return f"""---
id: {key.casefold()}_id
title: {title}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-08-09
sources: [raw/test.md]
{aliases_yaml}---
# [[{key}]]

## 1. 编译事实

### 物理机制 (Mechanism)
- [[{key}]] is a test concept. {extra_body}

## 2. 证据时间线
"""


def _canonical_rows(nodes):
    return [
        (
            key,
            str(node.get("title") or ""),
            json.dumps(node.get("aliases") or []),
        )
        for key, node in nodes.items()
    ]


def test_schema_rejects_category_outside_controlled_vocabulary():
    with pytest.raises(SchemaViolationException, match="Invalid category"):
        validate_schema(
            _source_frontmatter(["Knowledge_Governance"]),
            "",
            "Source_Invalid-Category.md",
        )


def test_source_link_closure_accepts_existing_and_same_batch_targets():
    files = [
        {
            "filename": "Source_Test.md",
            "content": _bare_content(
                "[[Concept_Current]] [[Current Alias]] [[Vendor_New.md]]",
                title="Source Test",
                node_type="source",
            ),
        },
        {
            "filename": "Vendor_New.md",
            "content": _bare_content(
                "body",
                title="New Vendor",
                node_type="vendor",
            ),
        },
    ]
    prepared = tool_ingest._prepare_source_link_closure(files)
    rows = _canonical_rows(
        {
            "Concept_Current": {
                "title": "Current Concept",
                "aliases": ["Current Alias"],
            }
        }
    )

    tool_ingest._assert_source_link_closure(prepared, rows)


def test_source_link_closure_rejects_missing_and_fuzzy_near_match():
    files = [
        {
            "filename": "Source_Test.md",
            "content": _bare_content(
                "[[Concept_Ambient-Scribing|display text]]",
                title="Source Test",
                node_type="source",
            ),
        }
    ]
    prepared = tool_ingest._prepare_source_link_closure(files)
    rows = _canonical_rows(
        {
            "Concept_Ambient-Scribes": {
                "title": "Ambient Scribes",
                "aliases": [],
            }
        }
    )

    with pytest.raises(
        tool_ingest.SourceLinkClosureError,
        match=r"Source_Test\.md -> \[\[Concept_Ambient-Scribing\]\] \(missing\)",
    ):
        tool_ingest._assert_source_link_closure(prepared, rows)


def test_source_link_closure_rejects_ambiguous_alias():
    prepared = tool_ingest._prepare_source_link_closure(
        [
            {
                "filename": "Source_Test.md",
                "content": _bare_content(
                    "[[Shared Alias]]",
                    title="Source Test",
                    node_type="source",
                ),
            }
        ]
    )
    rows = _canonical_rows(
        {
            "Concept_A": {"title": "A", "aliases": ["Shared Alias"]},
            "Concept_B": {"title": "B", "aliases": ["Shared Alias"]},
        }
    )

    with pytest.raises(
        tool_ingest.SourceLinkClosureError,
        match="ambiguous: Concept_A, Concept_B",
    ):
        tool_ingest._assert_source_link_closure(prepared, rows)


def test_source_link_closure_removes_replaced_page_old_aliases():
    prepared = tool_ingest._prepare_source_link_closure(
        [
            {
                "filename": "Source_Test.md",
                "content": _bare_content(
                    "[[Retired Alias]]",
                    title="Source Test",
                    node_type="source",
                ),
            },
            {
                "filename": "Concept_Target.md",
                "content": _bare_content(
                    "body",
                    title="New Title",
                    node_type="concept",
                ),
            },
        ]
    )
    rows = _canonical_rows(
        {
            "Concept_Target": {
                "title": "Old Title",
                "aliases": ["Retired Alias"],
            }
        }
    )

    with pytest.raises(tool_ingest.SourceLinkClosureError, match="Retired Alias"):
        tool_ingest._assert_source_link_closure(prepared, rows)


def test_source_link_closure_ignores_code_temporal_and_non_source_content():
    files = [
        {
            "filename": "Source_Test.md",
            "content": _bare_content(
                "`[[Concept_Inline]]`\n"
                "```md\n[[Concept_Fenced]]\n```\n"
                "[[2026-08-09]]",
                title="Source Test",
                node_type="source",
            ),
        },
        {
            "filename": "Concept_Legacy.md",
            "content": _bare_content(
                "[[Concept_Legacy-Missing]]",
                title="Legacy Concept",
                node_type="concept",
            ),
        },
    ]
    prepared = tool_ingest._prepare_source_link_closure(files)

    tool_ingest._assert_source_link_closure(prepared, [])


def test_source_link_precondition_failure_has_no_mutation_side_effects(
    isolated_memory,
):
    content = _valid_source_content("[[Concept_Missing]]")
    precondition = tool_ingest._prepare_source_link_precondition(
        [{"filename": "Source_Missing.md", "content": content}]
    )
    db_store.init_db()
    conn = db_store.get_connection()
    before_outbox = conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0]
    before_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    with pytest.raises(tool_ingest.SourceLinkClosureError):
        execute_mutation_batch(
            [{"filename": "Source_Missing.md", "content": content}],
            validation_mode="schema",
            precondition_callback=precondition,
        )

    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == before_outbox
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == before_entities
    assert not (isolated_memory / "wiki" / "Source_Missing.md").exists()


def test_source_link_precondition_accepts_same_batch_new_target(isolated_memory):
    source = _valid_source_content("[[Concept_New]]", entity_id="source_new")
    concept = _valid_concept_content(
        "Concept_New",
        title="New Concept",
        extra_body="[[Source_New]]",
    )
    files = [
        {"filename": "Source_New.md", "content": source},
        {"filename": "Concept_New.md", "content": concept},
    ]
    precondition = tool_ingest._prepare_source_link_precondition(files)

    execute_mutation_batch(
        files,
        validation_mode="schema",
        precondition_callback=precondition,
    )

    assert (isolated_memory / "wiki" / "Source_New.md").exists()
    assert (isolated_memory / "wiki" / "Concept_New.md").exists()


def test_source_link_precondition_observes_target_deleted_after_prepare(
    isolated_memory,
):
    target_content = _valid_concept_content(
        "Concept_Target",
        title="Target Concept",
    )
    execute_mutation_batch(
        [{"filename": "Concept_Target.md", "content": target_content}],
        validation_mode="schema",
    )
    source_content = _valid_source_content("[[Concept_Target]]", entity_id="source_late")
    precondition = tool_ingest._prepare_source_link_precondition(
        [{"filename": "Source_Late.md", "content": source_content}]
    )
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_batch(
        [
            {
                "filename": "Concept_Target.md",
                "is_delete": True,
                "expected_projection_hash": hashlib.sha256(
                    target_path.read_bytes()
                ).hexdigest(),
            }
        ],
        validation_mode="schema",
    )
    conn = db_store.get_connection()
    before_outbox = conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0]

    with pytest.raises(tool_ingest.SourceLinkClosureError, match="Concept_Target"):
        execute_mutation_batch(
            [{"filename": "Source_Late.md", "content": source_content}],
            validation_mode="schema",
            precondition_callback=precondition,
        )

    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == before_outbox
    assert not (isolated_memory / "wiki" / "Source_Late.md").exists()
    assert "Source_Late" not in governance_store.canonical_page_versions(
        {"Source_Late"}
    )
