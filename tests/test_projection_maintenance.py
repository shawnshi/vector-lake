from vector_lake import db_store, governance_store, indexer
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.tool_projection import (
    canonical_backfill_missing_wiki,
    reconcile_canonical_content_from_wiki,
    create_maintenance_backup,
    projection_diff_report,
    rebuild_index_projection,
    restore_missing_wiki_from_canonical,
)
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path
import pytest

from vector_lake.watchdog_app import WikiIndexHandler, index_queue, process_mutation_outbox_batch


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


def test_reconcile_canonical_content_from_richer_wiki_projection(isolated_memory):
    _purpose(isolated_memory)
    original = _page("Reconcile Content")
    execute_mutation_plan("Concept_Reconcile-Content.md", content=original)
    assert process_mutation_outbox_batch(limit=5)["completed"] == 1
    target = isolated_memory / "wiki" / "Concept_Reconcile-Content.md"
    richer = original.replace(
        "Reconcile Content body.",
        "Reconcile Content body with preserved historical detail.",
    )
    target.write_text(richer, encoding="utf-8")
    before = governance_store.canonical_page_versions({"Concept_Reconcile-Content"})

    preview = reconcile_canonical_content_from_wiki(dry_run=True, limit=0, batch_size=10)

    assert "[DRY RUN]" in preview
    assert "drift_pages: 1" in preview
    assert governance_store.canonical_page_versions({"Concept_Reconcile-Content"}) == before

    applied = reconcile_canonical_content_from_wiki(
        dry_run=False,
        limit=0,
        batch_size=10,
    )

    assert "Reconciled 1 wiki page" in applied
    assert "outbox_completed=1" in applied
    assert target.read_text(encoding="utf-8") == richer
    assert governance_store.canonical_page_versions({"Concept_Reconcile-Content"}) == {
        "Concept_Reconcile-Content": governance_store.canonical_page_version_from_content(
            target.name,
            richer,
        )
    }
    assert process_mutation_outbox_batch(limit=5)["completed"] == 0


def test_restore_refuses_lossy_canonical_metadata(isolated_memory):
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
    assert "Restored 0 missing wiki page" in applied
    assert "unsafe-version=Concept_Restore-Me" in applied
    restored = isolated_memory / "wiki" / "Concept_Restore-Me.md"
    assert not restored.exists()


def test_restore_is_projection_only_repeatable_and_watchdog_managed(isolated_memory):
    _purpose(isolated_memory)
    original = _page("Repeatable Restore")
    execute_mutation_plan("Concept_Repeatable-Restore.md", content=original)
    target = isolated_memory / "wiki" / "Concept_Repeatable-Restore.md"
    canonical_tables = ("entities", "claims", "evidence", "sources")

    def snapshot_canonical():
        conn = db_store.get_connection()
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in canonical_tables
        }

    while not index_queue.empty():
        index_queue.get_nowait()
        index_queue.task_done()
    before = snapshot_canonical()
    version_before = governance_store.canonical_page_versions({"Concept_Repeatable-Restore"})

    target.unlink()
    first = restore_missing_wiki_from_canonical(dry_run=False, limit=5)
    assert "Restored 1 missing wiki page" in first
    first_id = db_store.get_connection().execute(
        "SELECT id FROM mutation_outbox WHERE filename = 'Concept_Repeatable-Restore.md' "
        "AND status = 'pending' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    WikiIndexHandler().queue_path(str(target))
    assert index_queue.empty()
    assert snapshot_canonical() == before
    assert governance_store.canonical_page_versions({"Concept_Repeatable-Restore"}) == version_before
    assert governance_store.canonical_page_version_from_content(
        "Concept_Repeatable-Restore.md",
        target.read_text(encoding="utf-8"),
    ) == version_before["Concept_Repeatable-Restore"]

    assert process_mutation_outbox_batch(limit=5)["completed"] == 1
    target.unlink()
    second = restore_missing_wiki_from_canonical(dry_run=False, limit=5)
    assert "Restored 1 missing wiki page" in second
    second_id = db_store.get_connection().execute(
        "SELECT id FROM mutation_outbox WHERE filename = 'Concept_Repeatable-Restore.md' "
        "AND status = 'pending' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert second_id > first_id
    assert snapshot_canonical() == before
    assert governance_store.canonical_page_versions({"Concept_Repeatable-Restore"}) == version_before


def test_maintenance_backup_is_queryable_and_has_manifest(isolated_memory):
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_backup",
        {"entity_id": "entity_backup", "canonical_name": "Backup", "page_key": "Concept_Backup"},
    )

    backup_dir = Path(create_maintenance_backup("test_backup"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    backup_db = backup_dir / "vector_lake.db"
    with closing(sqlite3.connect(backup_db)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_id = 'entity_backup'"
        ).fetchone()[0]

    assert "vector_lake.db" in manifest["copied"]
    assert count == 1


def _backup_entries():
    from vector_lake.wiki_utils import get_meta_dir

    backup_root = get_meta_dir() / "backups"
    if not backup_root.exists():
        return []
    return list(backup_root.iterdir())


def test_maintenance_backup_copies_one_projection_generation(isolated_memory):
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    indexer.generate_index()
    source_index = json.loads(get_index_path().read_text(encoding="utf-8"))
    source_graph = json.loads(get_claim_graph_path().read_text(encoding="utf-8"))
    source_generation = indexer.validate_projection_pair(source_index, source_graph)
    source_canonical_generation = indexer.projection_canonical_generation(
        source_index,
        source_graph,
    )

    backup_dir = Path(create_maintenance_backup("projection_pair"))
    backup_index = json.loads((backup_dir / "index.json").read_text(encoding="utf-8"))
    backup_graph = json.loads(
        (backup_dir / "claim_graph.json").read_text(encoding="utf-8")
    )
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

    assert indexer.validate_projection_pair(backup_index, backup_graph) == source_generation
    assert manifest["manifest_version"] == 3
    assert manifest["projection_generation"] == source_generation
    assert manifest["projection_canonical_generation"] == source_canonical_generation
    assert manifest["database_runtime_generation_error"] is None
    assert manifest["canonical_projection_consistency"]["status"] == "verified"
    assert manifest["canonical_projection_consistency"][
        "canonical_generation_token"
    ] == source_canonical_generation["token"]
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is True
    assert manifest["complete"] is True
    assert {"index.json", "claim_graph.json"} <= set(manifest["copied"])
    assert set(manifest["artifact_sha256"]) == set(manifest["copied"])
    for name, expected_hash in manifest["artifact_sha256"].items():
        assert hashlib.sha256((backup_dir / name).read_bytes()).hexdigest() == (
            expected_hash
        )


def test_maintenance_backup_marks_stale_projection_generation_unverifiable(
    isolated_memory,
):
    db_store.init_db()
    indexer.generate_index()
    governance_store.upsert_entity(
        "entity_after_projection",
        {
            "entity_id": "entity_after_projection",
            "canonical_name": "After Projection",
            "page_key": "Concept_After-Projection",
        },
    )

    backup_dir = Path(create_maintenance_backup("stale_projection"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    consistency = manifest["canonical_projection_consistency"]

    assert consistency["status"] == "unverifiable"
    assert consistency["reason"] == (
        "backup-and-projection-runtime-generations-do-not-match"
    )
    assert consistency["database_runtime_generations"]["entities"] > (
        consistency["projection_runtime_generations"]["entities"]
    )
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is False
    assert manifest["complete"] is True


def test_maintenance_backup_detects_stale_governance_metrics(
    isolated_memory,
):
    db_store.init_db()
    indexer.generate_index()
    governance_store.upsert_governance_item(
        {
            "item_id": "governance_after_projection",
            "type": "review",
            "status": "pending",
        }
    )

    backup_dir = Path(create_maintenance_backup("stale_governance_metrics"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    consistency = manifest["canonical_projection_consistency"]

    assert consistency["status"] == "unverifiable"
    assert consistency["reason"] == (
        "backup-and-projection-runtime-generations-do-not-match"
    )
    assert consistency["database_runtime_generations"]["governance_queue"] > (
        consistency["projection_runtime_generations"]["governance_queue"]
    )
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is False


def test_maintenance_backup_compares_projection_to_copied_database_snapshot(
    isolated_memory,
    monkeypatch,
):
    import vector_lake.tool_projection as tool_projection

    db_store.init_db()
    indexer.generate_index()
    real_backup = tool_projection.backup_database

    def backup_then_advance_live_database(destination):
        real_backup(destination)
        governance_store.upsert_entity(
            "entity_after_database_copy",
            {
                "entity_id": "entity_after_database_copy",
                "canonical_name": "After Database Copy",
                "page_key": "Concept_After-Database-Copy",
            },
        )

    monkeypatch.setattr(
        tool_projection,
        "backup_database",
        backup_then_advance_live_database,
    )

    backup_dir = Path(create_maintenance_backup("database_copy_snapshot"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    live_entity_generation = db_store.get_connection().execute(
        "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
    ).fetchone()[0]

    assert manifest["canonical_projection_consistency"]["status"] == "verified"
    assert manifest["database_runtime_generations"]["entities"] < (
        live_entity_generation
    )
    assert manifest[
        "restorable_as_consistent_canonical_projection_snapshot"
    ] is True


def test_maintenance_backup_rejects_legacy_unbound_projection(
    isolated_memory,
):
    from vector_lake.wiki_utils import get_claim_graph_path, get_index_path

    db_store.init_db()
    indexer.generate_index()
    for path in (get_index_path(), get_claim_graph_path()):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[indexer.PROJECTION_MANIFEST_KEY].pop("canonical_generation")
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="no canonical_generation",
    ):
        create_maintenance_backup("legacy_unbound")

    assert _backup_entries() == []


@pytest.mark.parametrize("legacy_shape", ["manifestless_pair", "claim_topology"])
def test_maintenance_backup_rejects_legacy_projection_without_partial_backup(
    isolated_memory,
    legacy_shape,
):
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_legacy_claim_graph_path,
    )

    db_store.init_db()
    get_index_path().parent.mkdir(parents=True, exist_ok=True)
    if legacy_shape == "manifestless_pair":
        get_index_path().write_text(
            json.dumps({"nodes": {}, "weighted_edges": []}),
            encoding="utf-8",
        )
        get_claim_graph_path().write_text(
            json.dumps({"nodes": [], "edges": []}),
            encoding="utf-8",
        )
        error_pattern = "Legacy index/claim-graph projections"
    else:
        get_legacy_claim_graph_path().write_text(
            json.dumps({"nodes": [], "edges": []}),
            encoding="utf-8",
        )
        error_pattern = "Legacy claim_topology.json"

    with pytest.raises(indexer.ProjectionPairContractError, match=error_pattern):
        create_maintenance_backup(f"legacy_{legacy_shape}")

    assert _backup_entries() == []


def test_maintenance_backup_rejects_mismatched_generation_without_partial_backup(
    isolated_memory,
):
    from vector_lake.wiki_utils import get_claim_graph_path

    db_store.init_db()
    indexer.generate_index()
    claim_graph_path = get_claim_graph_path()
    claim_graph = json.loads(claim_graph_path.read_text(encoding="utf-8"))
    claim_graph[indexer.PROJECTION_MANIFEST_KEY]["generation"] = "mismatched"
    claim_graph_path.write_text(json.dumps(claim_graph), encoding="utf-8")

    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="generations do not match",
    ):
        create_maintenance_backup("mismatched")

    assert _backup_entries() == []


def test_maintenance_backup_rejects_half_published_pair_without_partial_backup(
    isolated_memory,
):
    from vector_lake.wiki_utils import get_claim_graph_path

    db_store.init_db()
    indexer.generate_index()
    get_claim_graph_path().unlink()

    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="projection pair is incomplete",
    ):
        create_maintenance_backup("half_published")

    assert _backup_entries() == []


def test_maintenance_backup_copies_projection_files_inside_publish_lock(
    isolated_memory,
    monkeypatch,
):
    import vector_lake.tool_projection as tool_projection
    from vector_lake.wiki_utils import get_index_path

    db_store.init_db()
    indexer.generate_index()
    state = {"inside": False, "lock": None, "projection_copies": 0}

    class RecordingLock:
        def __init__(self, lock_path, timeout):
            state["lock"] = (lock_path, timeout)

        def __enter__(self):
            state["inside"] = True
            return self

        def __exit__(self, exc_type, exc, traceback):
            state["inside"] = False
            return False

    real_copy = tool_projection.shutil.copy2

    def guarded_copy(source, destination):
        if Path(source).name in {"index.json", "claim_graph.json"}:
            assert state["inside"] is True
            state["projection_copies"] += 1
        return real_copy(source, destination)

    monkeypatch.setattr(tool_projection, "FileLock", RecordingLock)
    monkeypatch.setattr(tool_projection.shutil, "copy2", guarded_copy)

    backup_dir = Path(create_maintenance_backup("locked_copy"))

    assert state["lock"] == (str(get_index_path()) + ".lock", 15)
    assert state["projection_copies"] == 2
    assert state["inside"] is False
    assert (backup_dir / "index.json").exists()
    assert (backup_dir / "claim_graph.json").exists()


def test_maintenance_backup_removes_stage_when_projection_copy_fails(
    isolated_memory,
    monkeypatch,
):
    import vector_lake.tool_projection as tool_projection

    db_store.init_db()
    indexer.generate_index()
    real_copy = tool_projection.shutil.copy2

    def fail_claim_graph_copy(source, destination):
        if Path(source).name == "claim_graph.json":
            raise OSError("injected projection copy failure")
        return real_copy(source, destination)

    monkeypatch.setattr(tool_projection.shutil, "copy2", fail_claim_graph_copy)

    with pytest.raises(OSError, match="injected projection copy failure"):
        create_maintenance_backup("copy_failure")

    assert _backup_entries() == []
