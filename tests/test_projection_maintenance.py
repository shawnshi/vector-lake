from vector_lake import (
    db_store,
    governance_store,
    indexer,
    tool_backup_retention,
    tool_projection,
)
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
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import time
import weakref
from pathlib import Path
import pytest

from vector_lake.watchdog_app import (
    WikiIndexHandler,
    index_queue,
    process_mutation_outbox_batch,
)


def _set_backup_created_at(path: Path, timestamp: float) -> None:
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


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
id: {title.lower().replace(" ", "_")}
title: {title}
type: {page_type}
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
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
    assert "topology_refreshed=True" in applied
    assert (isolated_memory / "wiki" / "index.json").exists()
    committed = indexer.read_committed_index_snapshot()
    assert committed["graph_state"]["dirty"] is False
    assert indexer.projection_pair_matches_current_generation() is True


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

    preview = reconcile_canonical_content_from_wiki(
        dry_run=True, limit=0, batch_size=10
    )

    assert "[DRY RUN]" in preview
    assert "drift_pages: 1" in preview
    assert (
        governance_store.canonical_page_versions({"Concept_Reconcile-Content"})
        == before
    )

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
            table: [
                tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in canonical_tables
        }

    while not index_queue.empty():
        index_queue.get_nowait()
        index_queue.task_done()
    before = snapshot_canonical()
    version_before = governance_store.canonical_page_versions(
        {"Concept_Repeatable-Restore"}
    )

    target.unlink()
    first = restore_missing_wiki_from_canonical(dry_run=False, limit=5)
    assert "Restored 1 missing wiki page" in first
    first_id = (
        db_store.get_connection()
        .execute(
            "SELECT id FROM mutation_outbox WHERE filename = 'Concept_Repeatable-Restore.md' "
            "AND status = 'pending' ORDER BY id DESC LIMIT 1"
        )
        .fetchone()[0]
    )
    WikiIndexHandler().queue_path(str(target))
    assert index_queue.empty()
    assert snapshot_canonical() == before
    assert (
        governance_store.canonical_page_versions({"Concept_Repeatable-Restore"})
        == version_before
    )
    assert (
        governance_store.canonical_page_version_from_content(
            "Concept_Repeatable-Restore.md",
            target.read_text(encoding="utf-8"),
        )
        == version_before["Concept_Repeatable-Restore"]
    )

    assert process_mutation_outbox_batch(limit=5)["completed"] == 1
    target.unlink()
    second = restore_missing_wiki_from_canonical(dry_run=False, limit=5)
    assert "Restored 1 missing wiki page" in second
    second_id = (
        db_store.get_connection()
        .execute(
            "SELECT id FROM mutation_outbox WHERE filename = 'Concept_Repeatable-Restore.md' "
            "AND status = 'pending' ORDER BY id DESC LIMIT 1"
        )
        .fetchone()[0]
    )
    assert second_id > first_id
    assert snapshot_canonical() == before
    assert (
        governance_store.canonical_page_versions({"Concept_Repeatable-Restore"})
        == version_before
    )


@pytest.mark.parametrize(
    "label",
    [
        "",
        " ",
        "../escape",
        "nested/escape",
        r"nested\escape",
        ".hidden",
        "label.with.dot",
        "x" * 65,
        "备份",
    ],
)
def test_maintenance_backup_rejects_unsafe_label_before_writing(
    isolated_memory,
    label,
):
    with pytest.raises(ValueError, match="backup label must match"):
        create_maintenance_backup(label)

    assert not (isolated_memory / "wiki" / ".meta" / "backups").exists()


def test_maintenance_backup_is_queryable_and_has_manifest(isolated_memory):
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_backup",
        {
            "entity_id": "entity_backup",
            "canonical_name": "Backup",
            "page_key": "Concept_Backup",
        },
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


def test_maintenance_backup_streams_hash_and_fsyncs_files(
    isolated_memory,
    monkeypatch,
):
    import vector_lake.tool_projection as tool_projection

    db_store.init_db()
    real_fsync = tool_projection.os.fsync
    sync_calls = []

    def reject_read_bytes(_path):
        raise AssertionError("maintenance backup must stream artifact hashes")

    def recording_fsync(file_descriptor):
        real_fsync(file_descriptor)
        sync_calls.append(file_descriptor)

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
    monkeypatch.setattr(tool_projection.os, "fsync", recording_fsync)

    backup_dir = Path(create_maintenance_backup("streamed_hash"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["copied"] == ["vector_lake.db"]
    assert set(manifest["artifact_sha256"]) == {"vector_lake.db"}
    assert len(sync_calls) == len(manifest["copied"]) + 1


def test_maintenance_backup_fsync_failure_does_not_publish(
    isolated_memory,
    monkeypatch,
):
    import vector_lake.tool_projection as tool_projection

    db_store.init_db()

    def fail_fsync(_file_descriptor):
        raise OSError("injected backup fsync failure")

    monkeypatch.setattr(tool_projection.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="injected backup fsync failure"):
        create_maintenance_backup("fsync_failure")

    assert _backup_entries() == []


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

    assert (
        indexer.validate_projection_pair(backup_index, backup_graph)
        == source_generation
    )
    assert manifest["manifest_version"] == 3
    assert manifest["projection_generation"] == source_generation
    assert manifest["projection_canonical_generation"] == source_canonical_generation
    assert manifest["database_runtime_generation_error"] is None
    assert manifest["canonical_projection_consistency"]["status"] == "verified"
    assert (
        manifest["canonical_projection_consistency"]["canonical_generation_token"]
        == source_canonical_generation["token"]
    )
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is True
    assert manifest["complete"] is True
    assert {
        "index.json",
        "claim_graph.json",
        "projection_pair_manifest.json",
    } <= set(manifest["copied"])
    assert set(manifest["artifact_sha256"]) == set(manifest["copied"])
    for name, expected_hash in manifest["artifact_sha256"].items():
        assert hashlib.sha256((backup_dir / name).read_bytes()).hexdigest() == (
            expected_hash
        )


def test_projection_backup_releases_source_payloads_before_copy_validation(
    isolated_memory,
    monkeypatch,
):
    class TrackedDict(dict):
        __slots__ = ("__weakref__",)

    db_store.init_db()
    indexer.generate_index()
    backup_dir = isolated_memory / "wiki" / ".meta" / "projection-memory"
    backup_dir.mkdir(parents=True)
    real_load = tool_projection.json.load
    roots = []
    peak_live_roots = 0

    def tracking_load(handle):
        nonlocal peak_live_roots
        payload = TrackedDict(real_load(handle))
        roots.append(weakref.ref(payload))
        peak_live_roots = max(
            peak_live_roots,
            sum(reference() is not None for reference in roots),
        )
        return payload

    monkeypatch.setattr(tool_projection.json, "load", tracking_load)

    copied, generation, canonical_binding = (
        tool_projection._copy_projection_pair_to_backup(backup_dir)
    )

    assert set(copied) == {
        "index.json",
        "claim_graph.json",
        "projection_pair_manifest.json",
    }
    assert generation
    assert canonical_binding["status"] == "verified"
    assert peak_live_roots <= 2


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
    assert (
        consistency["database_runtime_generations"]["entities"]
        > (consistency["projection_runtime_generations"]["entities"])
    )
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is False
    assert manifest["complete"] is True


def test_maintenance_backup_marks_external_sqlite_write_unverifiable(
    isolated_memory,
):
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_external_projection",
        {
            "entity_id": "entity_external_projection",
            "canonical_name": "External Projection A",
            "page_key": "Concept_External-Projection",
        },
    )
    indexer.generate_index()

    raw = sqlite3.connect(db_store.get_db_path())
    try:
        raw.execute(
            "UPDATE entities SET canonical_name = ?, data_json = ? WHERE entity_id = ?",
            (
                "External Projection B",
                json.dumps(
                    {
                        "entity_id": "entity_external_projection",
                        "canonical_name": "External Projection B",
                        "page_key": "Concept_External-Projection",
                    }
                ),
                "entity_external_projection",
            ),
        )
        raw.commit()
    finally:
        raw.close()

    backup_dir = Path(create_maintenance_backup("external_stale_projection"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    consistency = manifest["canonical_projection_consistency"]

    assert consistency["status"] == "unverifiable"
    assert consistency["reason"] == (
        "backup-and-projection-runtime-generations-do-not-match"
    )
    assert (
        consistency["database_runtime_generations"]["entities"]
        > (consistency["projection_runtime_generations"]["entities"])
    )
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is False


def test_maintenance_backup_marks_damaged_generation_schema_unverifiable(
    isolated_memory,
):
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_damaged_generation",
        {
            "entity_id": "entity_damaged_generation",
            "canonical_name": "Before Trigger Damage",
            "page_key": "Concept_Damaged-Generation",
        },
    )
    indexer.generate_index()
    trigger_name = db_store._runtime_generation_trigger_name(
        "entities",
        "update",
    )
    raw = sqlite3.connect(db_store.get_db_path())
    try:
        generation_before = raw.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
        raw.execute(f"DROP TRIGGER {trigger_name}")
        raw.execute(
            "UPDATE entities SET canonical_name = ?, data_json = ? WHERE entity_id = ?",
            (
                "After Trigger Damage",
                json.dumps(
                    {
                        "entity_id": "entity_damaged_generation",
                        "canonical_name": "After Trigger Damage",
                        "page_key": "Concept_Damaged-Generation",
                    }
                ),
                "entity_damaged_generation",
            ),
        )
        raw.commit()
        generation_after = raw.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    finally:
        raw.close()

    assert generation_after == generation_before
    backup_dir = Path(create_maintenance_backup("damaged_generation_schema"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    error = manifest["database_runtime_generation_error"]

    assert manifest["database_runtime_generations"] is None
    assert error.startswith("backup-schema-invalid:")
    assert f"runtime_generation_schema_missing:trigger:{trigger_name}" in error
    assert manifest["canonical_projection_consistency"] == {
        "status": "unverifiable",
        "reason": error,
        "verification_scope": "tracked-canonical-projection-surfaces",
        "covered_surfaces": list(indexer.CANONICAL_PROJECTION_SURFACES),
    }
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is False
    assert not (backup_dir / "vector_lake.db-wal").exists()
    assert not (backup_dir / "vector_lake.db-shm").exists()


def test_published_backup_normal_read_only_open_stays_retention_eligible(
    isolated_memory,
):
    db_store.init_db()
    older = Path(create_maintenance_backup("normal_ro_older"))
    newer = Path(create_maintenance_backup("normal_ro_newer"))
    backup_database = older / "vector_lake.db"

    with closing(
        sqlite3.connect(
            f"{backup_database.resolve().as_uri()}?mode=ro",
            uri=True,
        )
    ) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0

    assert not (older / "vector_lake.db-wal").exists()
    assert not (older / "vector_lake.db-shm").exists()
    now = time.time()
    os.utime(older, (now - 90 * 86400, now - 90 * 86400))
    os.utime(newer, (now - 60 * 86400, now - 60 * 86400))
    _set_backup_created_at(older, now - 90 * 86400)
    _set_backup_created_at(newer, now - 60 * 86400)
    retention = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert {(item["name"], item["type"]) for item in retention["candidates"]} == {
        (older.name, "complete_backup")
    }
    assert {item["name"] for item in retention["protected"]} == {newer.name}
    assert retention["ignored"] == []


def test_custom_database_path_backup_uses_stable_name_and_is_retention_eligible(
    isolated_memory,
    monkeypatch,
):
    custom_database = isolated_memory / "custom-db" / "custom.sqlite"
    custom_database.parent.mkdir()
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(custom_database))
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_custom_database",
        {
            "entity_id": "entity_custom_database",
            "canonical_name": "Custom Database",
            "page_key": "Concept_Custom-Database",
        },
    )
    older = Path(create_maintenance_backup("custom_path_older"))
    newer = Path(create_maintenance_backup("custom_path_newer"))

    assert custom_database.exists()
    for backup_dir in (older, newer):
        manifest = json.loads(
            (backup_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["copied"] == ["vector_lake.db"]
        assert set(manifest["artifact_sha256"]) == {"vector_lake.db"}
        assert (backup_dir / "vector_lake.db").is_file()
        assert not (backup_dir / custom_database.name).exists()
        with closing(
            sqlite3.connect(
                f"{(backup_dir / 'vector_lake.db').resolve().as_uri()}?mode=ro",
                uri=True,
            )
        ) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM entities WHERE entity_id = ?",
                    ("entity_custom_database",),
                ).fetchone()[0]
                == 1
            )

    now = time.time()
    os.utime(older, (now - 90 * 86400, now - 90 * 86400))
    os.utime(newer, (now - 60 * 86400, now - 60 * 86400))
    _set_backup_created_at(older, now - 90 * 86400)
    _set_backup_created_at(newer, now - 60 * 86400)
    retention = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert {(item["name"], item["type"]) for item in retention["candidates"]} == {
        (older.name, "complete_backup")
    }
    assert {item["name"] for item in retention["protected"]} == {newer.name}
    assert retention["ignored"] == []


def test_maintenance_backup_ignores_soft_governance_generation_drift(
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

    assert consistency["status"] == "verified"
    assert consistency["reason"] == "runtime-generations-match"
    assert consistency["covered_surfaces"] == list(
        indexer.CANONICAL_PROJECTION_SURFACES
    )
    assert "governance_queue" not in consistency[
        "projection_runtime_generations"
    ]
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is True


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
    live_entity_generation = (
        db_store.get_connection()
        .execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        )
        .fetchone()[0]
    )

    assert manifest["canonical_projection_consistency"]["status"] == "verified"
    assert manifest["database_runtime_generations"]["entities"] < (
        live_entity_generation
    )
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is True


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


def test_maintenance_backup_synthesizes_missing_sidecar_only_inside_backup(
    isolated_memory,
):
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    indexer.generate_index()
    sidecar_path = get_projection_manifest_path()
    sidecar_path.unlink()

    backup = Path(create_maintenance_backup("missing_sidecar"))

    assert sidecar_path.exists() is False
    copied_sidecar = json.loads(
        (backup / sidecar_path.name).read_text(encoding="utf-8")
    )
    indexer._validate_projection_sidecar(copied_sidecar)


def test_maintenance_backup_rejects_hard_stale_pair_when_sidecar_is_missing(
    isolated_memory,
):
    from vector_lake import governance_store
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    indexer.generate_index()
    get_projection_manifest_path().unlink()
    governance_store.upsert_entity(
        "entity_backup_stale_pair",
        {
            "entity_id": "entity_backup_stale_pair",
            "canonical_name": "Backup Stale Pair",
            "entity_type": "concept",
            "page_key": "Concept_Backup-Stale-Pair",
        },
    )

    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="binding is stale",
    ):
        create_maintenance_backup("missing_stale_sidecar")
    assert _backup_entries() == []


def test_maintenance_backup_rejects_tampered_sidecar(
    isolated_memory,
):
    from vector_lake.wiki_utils import get_index_path

    db_store.init_db()
    indexer.generate_index()
    index_payload = json.loads(get_index_path().read_text(encoding="utf-8"))
    index_payload["nodes"]["Concept_Tampered"] = {"title": "Tampered"}
    get_index_path().write_text(json.dumps(index_payload), encoding="utf-8")
    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="sidecar digest does not match index.json",
    ):
        create_maintenance_backup("tampered_sidecar")
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
