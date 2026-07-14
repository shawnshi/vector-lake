from vector_lake import db_store, governance_store, indexer
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.tool_projection import (
    canonical_backfill_missing_wiki,
    create_maintenance_backup,
    projection_diff_report,
    rebuild_index_projection,
    restore_missing_wiki_from_canonical,
)
import json
import sqlite3
from pathlib import Path


def _purpose(memory_dir):
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.0"
intent_keywords: [test]
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
sir_registry:
  - id: SIR_TEST
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Test purpose.
""",
        encoding="utf-8",
    )


def _page(title: str, page_type: str = "concept") -> str:
    return f"""---
id: {title.lower().replace(' ', '_')}
title: {title}
type: {page_type}
domain: General
status: Active
epistemic-status: seed
categories: [Concept]
updated: 2026-07-13T00:00:00+00:00
sources: []
strategic_scope: core
evidence_tier: primary
---
## 1. 编译事实

### 物理机制 (Mechanism)

{title} body.

## 2. 证据时间线

- [2026-07-13] [Observation] {title} observed in maintenance test.
"""


def test_projection_report_and_backfill_missing_canonical(isolated_memory):
    _purpose(isolated_memory)
    execute_mutation_plan("Concept_Existing.md", content=_page("Existing"))
    indexer.generate_index()

    orphan_path = isolated_memory / "wiki" / "Concept_Orphan.md"
    orphan_path.write_text(_page("Orphan"), encoding="utf-8")

    report = projection_diff_report(limit=5)
    assert "missing_canonical: 1" in report
    assert "Concept_Orphan" in report

    dry = canonical_backfill_missing_wiki(dry_run=True, limit=5)
    assert "[DRY RUN]" in dry
    assert "valid_pages: 1" in dry

    conn = db_store.get_connection()
    before = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE json_extract(data_json, '$.page_key') = 'Concept_Orphan'"
    ).fetchone()[0]
    assert before == 0

    applied = canonical_backfill_missing_wiki(dry_run=False, limit=5)
    assert "Backfilled 1 wiki page" in applied

    after = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE json_extract(data_json, '$.page_key') = 'Concept_Orphan'"
    ).fetchone()[0]
    assert after == 1


def test_rebuild_index_projection_dry_run_and_apply(isolated_memory):
    _purpose(isolated_memory)
    execute_mutation_plan("Concept_Existing.md", content=_page("Existing"))

    dry = rebuild_index_projection(dry_run=True)
    assert "[DRY RUN]" in dry

    applied = rebuild_index_projection(dry_run=False)
    assert "Rebuilt index projection" in applied
    assert (isolated_memory / "wiki" / "index.json").exists()


def test_restore_missing_wiki_from_canonical_metadata(isolated_memory):
    _purpose(isolated_memory)
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_restore",
        {
            "entity_id": "entity_restore",
            "canonical_name": "Restore Me",
            "entity_type": "concept",
            "status": "Active",
            "domain": "General",
            "page_key": "Concept_Restore-Me",
            "updated_at": "2026-07-13",
            "source_page": "Concept_Restore-Me.md",
        },
    )

    dry = restore_missing_wiki_from_canonical(dry_run=True, limit=5)
    assert "[DRY RUN]" in dry
    assert "Concept_Restore-Me" in dry

    applied = restore_missing_wiki_from_canonical(dry_run=False, limit=5)
    assert "Restored 1 missing wiki page" in applied
    restored = isolated_memory / "wiki" / "Concept_Restore-Me.md"
    assert restored.exists()
    content = restored.read_text(encoding="utf-8")
    assert "## 1. 编译事实" in content
    assert "## 2. 证据时间线" in content


def test_maintenance_backup_is_queryable_and_has_manifest(isolated_memory):
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_backup",
        {"entity_id": "entity_backup", "canonical_name": "Backup", "page_key": "Concept_Backup"},
    )

    backup_dir = Path(create_maintenance_backup("test_backup"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    backup_db = backup_dir / "vector_lake.db"
    with sqlite3.connect(backup_db) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_id = 'entity_backup'"
        ).fetchone()[0]

    assert "vector_lake.db" in manifest["copied"]
    assert count == 1
