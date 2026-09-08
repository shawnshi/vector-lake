import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from vector_lake import (
    db_store,
    governance_store,
    indexer,
    restore_snapshot,
    tool_projection,
)
from vector_lake.backup_capacity import (
    estimate_maintenance_backup_bytes,
    projection_v2_reachable_inventory,
)
from vector_lake.projection_store_v2 import ProjectionStoreV2


def _raw_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _seed_v2_backup() -> tuple[Path, dict]:
    db_store.init_db()
    indexer.generate_index()
    db_store.close_all_connections()
    directory = Path(tool_projection.create_maintenance_backup("projection_v2"))
    manifest, inventory = tool_projection.validate_maintenance_backup_v4(
        directory / "manifest.json"
    )
    assert inventory is not None
    return directory, manifest


def test_v4_backup_contains_exact_reachable_object_closure(isolated_memory):
    directory, manifest = _seed_v2_backup()
    inventory = projection_v2_reachable_inventory(wiki_dir=directory)

    assert manifest["manifest_version"] == 4
    assert manifest["projection_format"] == 2
    assert inventory is not None
    assert manifest["projection_v2"]["object_count"] == inventory["object_count"]
    assert set(manifest["artifact_sha256"]) == set(manifest["copied"])
    assert set(manifest["artifact_bytes"]) == set(manifest["copied"])
    for name in manifest["copied"]:
        artifact = directory / Path(name)
        assert _raw_sha256(artifact) == manifest["artifact_sha256"][name]
        assert artifact.stat().st_size == manifest["artifact_bytes"][name]


def test_v4_backup_rejects_missing_object_and_unknown_extra(isolated_memory):
    directory, manifest = _seed_v2_backup()
    object_name = manifest["projection_v2"]["object_artifacts"][0]
    (directory / Path(object_name)).unlink()
    with pytest.raises(ValueError, match="missing|invalid|unreadable"):
        tool_projection.validate_maintenance_backup_v4(directory / "manifest.json")

    directory, _manifest = _seed_v2_backup()
    (directory / "undeclared.bin").write_bytes(b"undeclared")
    with pytest.raises(ValueError, match="unknown"):
        tool_projection.validate_maintenance_backup_v4(directory / "manifest.json")


def test_v2_capacity_estimate_includes_reachable_closure(isolated_memory):
    db_store.init_db()
    indexer.generate_index()
    inventory = projection_v2_reachable_inventory()
    assert inventory is not None

    estimated = estimate_maintenance_backup_bytes()

    assert estimated >= inventory["total_projection_bytes"]


def test_v2_restore_crash_after_object_merge_resumes_idempotently(
    isolated_memory,
    monkeypatch,
):
    directory, manifest = _seed_v2_backup()
    receipt = directory / "manifest.json"
    target_generation = manifest["projection_generation"]

    sidecar_path = isolated_memory / "wiki" / "projection_pair_manifest.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["published_at_utc"] = "2026-08-28T12:00:00+00:00"
    sidecar_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    db_store.close_all_connections()

    preview = restore_snapshot.preview_restore_snapshot(receipt)
    assert preview["can_apply"] is True, preview
    assert preview["projection_action"] == "restore_committed_pair"

    def fail_after_merge(name: str) -> None:
        if name == "after_projection_object_merge":
            raise RuntimeError("simulated-process-death")

    monkeypatch.setattr(restore_snapshot, "_TEST_FAULT_HOOK", fail_after_merge)
    with pytest.raises(RuntimeError, match="simulated-process-death"):
        restore_snapshot.restore_snapshot_maintenance(
            maintenance_receipt=receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
        )

    monkeypatch.setattr(restore_snapshot, "_TEST_FAULT_HOOK", None)
    resumed = restore_snapshot.preview_restore_snapshot(receipt)
    assert resumed["pending_restore_receipt"] is not None, resumed
    result = restore_snapshot.restore_snapshot_maintenance(
        maintenance_receipt=receipt,
        apply=True,
        confirmation=resumed["fingerprint"],
        confirm_no_writers=True,
    )
    assert result["recovery_action"] == "resumed_and_completed_restore"
    live = projection_v2_reachable_inventory()
    assert live is not None
    assert live["projection_generation"] == target_generation

    final_preview = restore_snapshot.preview_restore_snapshot(receipt)
    no_op = restore_snapshot.restore_snapshot_maintenance(
        maintenance_receipt=receipt,
        apply=True,
        confirmation=final_preview["fingerprint"],
        confirm_no_writers=True,
    )
    assert no_op["no_op"] is True


def test_projection_object_gc_preview_apply_and_idempotent_receipt(
    isolated_memory,
):
    _directory, _manifest = _seed_v2_backup()
    store = ProjectionStoreV2(isolated_memory / "wiki")
    orphan = store.apply(None, sets={"orphan-only": {"value": "discard"}})
    old = time.time() - 10 * 86_400
    for path in store.object_paths(orphan.root_digest, max_objects=128):
        os.utime(path, (old, old))
    live_before = projection_v2_reachable_inventory()

    preview = tool_projection.projection_object_gc(
        dry_run=True,
        retention_days=7,
        limit=100,
    )
    assert preview["can_apply"] is True, preview
    assert orphan.root_digest in {item["sha256"] for item in preview["candidates"]}

    applied = tool_projection.projection_object_gc(
        dry_run=False,
        retention_days=7,
        limit=100,
        confirmation=preview["fingerprint"],
    )
    assert applied["deleted_objects"] >= 1
    assert store.object_path(orphan.root_digest).exists() is False
    assert (
        projection_v2_reachable_inventory()["sidecar_sha256"]
        == live_before["sidecar_sha256"]
    )

    repeated = tool_projection.projection_object_gc(
        dry_run=False,
        retention_days=7,
        limit=100,
        confirmation=preview["fingerprint"],
    )
    assert repeated["no_op"] is True


def test_projection_object_gc_partial_failure_resumes_same_receipt(
    isolated_memory,
    monkeypatch,
):
    _directory, _manifest = _seed_v2_backup()
    store = ProjectionStoreV2(isolated_memory / "wiki")
    first = store.apply(None, sets={"orphan-a": {"value": "a"}})
    second = store.apply(None, sets={"orphan-b": {"value": "b"}})
    old = time.time() - 10 * 86_400
    orphan_paths = {
        *store.object_paths(first.root_digest, max_objects=128),
        *store.object_paths(second.root_digest, max_objects=128),
    }
    for path in orphan_paths:
        os.utime(path, (old, old))
    preview = tool_projection.projection_object_gc(
        dry_run=True,
        retention_days=7,
        limit=100,
    )

    def fail_once(deleted: int, _digest: str) -> None:
        if deleted == 1:
            raise RuntimeError("gc-process-death")

    monkeypatch.setattr(
        tool_projection,
        "_TEST_PROJECTION_GC_FAULT_HOOK",
        fail_once,
    )
    with pytest.raises(RuntimeError, match="gc-process-death"):
        tool_projection.projection_object_gc(
            dry_run=False,
            retention_days=7,
            limit=100,
            confirmation=preview["fingerprint"],
        )
    monkeypatch.setattr(tool_projection, "_TEST_PROJECTION_GC_FAULT_HOOK", None)

    resumed = tool_projection.projection_object_gc(
        dry_run=False,
        retention_days=7,
        limit=100,
        confirmation=preview["fingerprint"],
    )
    assert resumed["resumed"] is True
    assert resumed["already_missing_objects"] == 1
    assert all(not path.exists() for path in orphan_paths)


def test_a02_reproduction_and_safe_recovery(isolated_memory):
    db_store.init_db()
    indexer.generate_index()
    state = db_store.get_projection_runtime_v9()
    assert state["status"] == "ready"

    with db_store.transaction() as conn:
        db_store.cas_projection_runtime_publish_pending(
            conn,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=state["projection_generation"],
            canonical_generation=state["canonical_generation"],
            sidecar_json=state["sidecar_json"],
        )

    pending_state = db_store.get_projection_runtime_v9()
    assert pending_state["status"] == "publish_pending"

    synthetic_change_set = {
        "change_set_id": "cs_synthetic_a02",
        "affected_pages": ["Concept_A02"],
        "proposed_claims": [
            {
                "claim_id": "claim:synthetic_a02",
                "claim_text": "Synthetic claim for A02 reproduction",
                "status": "Accepted",
                "locator": {"page_key": "Concept_A02"},
            }
        ],
    }
    governance_store.apply_change_set(synthetic_change_set)

    from vector_lake.projection_format_v2 import ProjectionV2ContractError

    with pytest.raises(ProjectionV2ContractError, match="pending_canonical_generation_stale"):
        indexer.generate_index()
    tool_projection.rebuild_index_projection(dry_run=False)
    final_state = db_store.get_projection_runtime_v9()
    assert final_state["status"] == "ready"
    assert final_state["projection_generation"] != state["projection_generation"]

    from vector_lake.projection_format_v2 import load_committed_claim_graph

    claim_graph = load_committed_claim_graph(isolated_memory / "wiki")
    nodes = {n["id"] for n in claim_graph.get("nodes", [])}
    assert "claim:synthetic_a02" in nodes


def test_a02_standalone_recover_pending_publish_preserves_obsolete_intent(
    isolated_memory,
):
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        load_committed_index,
        recover_pending_publish,
    )

    db_store.init_db()
    indexer.generate_index()
    state = db_store.get_projection_runtime_v9()
    initial_generation = state["projection_generation"]

    with db_store.transaction() as conn:
        db_store.cas_projection_runtime_publish_pending(
            conn,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=state["projection_generation"],
            canonical_generation=state["canonical_generation"],
            sidecar_json=state["sidecar_json"],
        )

    synthetic_change_set = {
        "change_set_id": "cs_synthetic_a02_standalone",
        "affected_pages": ["Concept_A02"],
        "proposed_claims": [
            {
                "claim_id": "claim:synthetic_a02_standalone",
                "claim_text": "Synthetic claim for A02 standalone test",
                "status": "Accepted",
                "locator": {"page_key": "Concept_A02"},
            }
        ],
    }
    governance_store.apply_change_set(synthetic_change_set)

    base = isolated_memory / "wiki"
    with pytest.raises(ProjectionV2ContractError, match="not_ready"):
        load_committed_index(base)

    pending = db_store.get_projection_runtime_v9()
    with pytest.raises(ProjectionV2ContractError, match="pending_canonical_generation_stale"):
        recover_pending_publish(base)
    assert db_store.get_projection_runtime_v9() == pending
    assert pending["projection_generation"] == initial_generation
    with pytest.raises(ProjectionV2ContractError, match="not_ready"):
        load_committed_index(base, require_current_generation=False)


def test_a02_refusal_of_tampered_pending_sidecar(isolated_memory):
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        recover_pending_publish,
    )

    db_store.init_db()
    indexer.generate_index()
    state = db_store.get_projection_runtime_v9()

    with db_store.transaction() as conn:
        db_store.cas_projection_runtime_publish_pending(
            conn,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=state["projection_generation"],
            canonical_generation=state["canonical_generation"],
            sidecar_json=state["sidecar_json"],
        )

    conn = db_store.get_connection()
    conn.execute(
        "UPDATE projection_runtime_v9 SET sidecar_sha256 = ? WHERE singleton = 1",
        ("0" * 64,),
    )
    conn.commit()

    base = isolated_memory / "wiki"
    with pytest.raises((ProjectionV2ContractError, RuntimeError)):
        recover_pending_publish(base)

    raw_status = conn.execute(
        "SELECT status FROM projection_runtime_v9 WHERE singleton = 1"
    ).fetchone()[0]
    assert raw_status == "publish_pending"


def test_a02_canonical_race_during_recovery(isolated_memory, monkeypatch):
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        recover_pending_publish,
    )

    db_store.init_db()
    indexer.generate_index()
    state = db_store.get_projection_runtime_v9()

    with db_store.transaction() as conn:
        db_store.cas_projection_runtime_publish_pending(
            conn,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=state["projection_generation"],
            canonical_generation=state["canonical_generation"],
            sidecar_json=state["sidecar_json"],
        )

    base = isolated_memory / "wiki"
    from vector_lake import projection_format_v2

    original_closure = projection_format_v2.validate_root_closure

    def race_closure(base_dir, sidecar):
        result = original_closure(base_dir, sidecar)
        governance_store.apply_change_set(
            {
                "change_set_id": "cs_race",
                "affected_pages": ["Concept_Race"],
                "proposed_claims": [
                    {
                        "claim_id": "claim:race",
                        "claim_text": "Race claim",
                        "status": "Accepted",
                        "locator": {"page_key": "Concept_Race"},
                    }
                ],
            }
        )
        return result

    monkeypatch.setattr(projection_format_v2, "validate_root_closure", race_closure)

    with pytest.raises(
        ProjectionV2ContractError, match="canonical_generation_changed_during_recovery"
    ):
        recover_pending_publish(base)

    monkeypatch.setattr(projection_format_v2, "validate_root_closure", original_closure)
    assert db_store.get_projection_runtime_v9()["status"] == "publish_pending"


def test_a02_obsolete_pending_fallback_to_rebuild_required_when_no_previous(
    isolated_memory,
):
    from vector_lake.projection_format_v2 import recover_pending_publish

    db_store.init_db()
    indexer.generate_index()
    state = db_store.get_projection_runtime_v9()

    with db_store.transaction() as conn:
        db_store.mark_projection_runtime_rebuild_required(conn)
        db_store.cas_projection_runtime_publish_pending(
            conn,
            expected_status="rebuild_required",
            expected_projection_generation=None,
            projection_generation=state["projection_generation"],
            canonical_generation=state["canonical_generation"],
            sidecar_json=state["sidecar_json"],
        )

    pending_state = db_store.get_projection_runtime_v9()
    assert pending_state["status"] == "publish_pending"
    assert pending_state["previous_sidecar_json"] is None

    governance_store.apply_change_set(
        {
            "change_set_id": "cs_synthetic_rebuild_req",
            "affected_pages": ["Concept_RebuildReq"],
            "proposed_claims": [
                {
                    "claim_id": "claim:synthetic_rebuild_req",
                    "claim_text": "Synthetic claim for rebuild_required test",
                    "status": "Accepted",
                    "locator": {"page_key": "Concept_RebuildReq"},
                }
            ],
        }
    )

    base = isolated_memory / "wiki"
    from vector_lake.projection_format_v2 import ProjectionV2ContractError

    with pytest.raises(ProjectionV2ContractError, match="pending_canonical_generation_stale"):
        recover_pending_publish(base)
    backup = Path(tool_projection.create_maintenance_backup("no_previous"))
    assert tool_projection._retire_pending_projection_from_backup(backup) is True
    assert db_store.get_projection_runtime_v9()["status"] == "rebuild_required"
    indexer.generate_index()
    assert db_store.get_projection_runtime_v9()["status"] == "ready"


def test_a02_maintenance_rebuild_index_preview_and_apply_under_pending(isolated_memory):
    db_store.init_db()
    indexer.generate_index()
    state = db_store.get_projection_runtime_v9()

    with db_store.transaction() as conn:
        db_store.cas_projection_runtime_publish_pending(
            conn,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=state["projection_generation"],
            canonical_generation=state["canonical_generation"],
            sidecar_json=state["sidecar_json"],
        )

    governance_store.apply_change_set(
        {
            "change_set_id": "cs_synthetic_preview_apply",
            "affected_pages": ["Concept_PreviewApply"],
            "proposed_claims": [
                {
                    "claim_id": "claim:synthetic_preview_apply",
                    "claim_text": "Synthetic claim for preview apply test",
                    "status": "Accepted",
                    "locator": {"page_key": "Concept_PreviewApply"},
                }
            ],
        }
    )

    preview = tool_projection.rebuild_index_projection(dry_run=True)
    assert "[DRY RUN]" in preview
    assert db_store.get_projection_runtime_v9()["status"] == "publish_pending"

    apply_result = tool_projection.rebuild_index_projection(dry_run=False)
    assert "Rebuilt index projection" in apply_result
    assert db_store.get_projection_runtime_v9()["status"] == "ready"


def test_a02_failure_leaves_recoverable_state(isolated_memory, monkeypatch):
    db_store.init_db()
    indexer.generate_index()
    state = db_store.get_projection_runtime_v9()

    with db_store.transaction() as conn:
        db_store.cas_projection_runtime_publish_pending(
            conn,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=state["projection_generation"],
            canonical_generation=state["canonical_generation"],
            sidecar_json=state["sidecar_json"],
        )

    governance_store.apply_change_set(
        {
            "change_set_id": "cs_fail_recoverable",
            "affected_pages": ["Concept_FailRecoverable"],
            "proposed_claims": [
                {
                    "claim_id": "claim:fail_recoverable",
                    "claim_text": "Synthetic claim for failure test",
                    "status": "Accepted",
                    "locator": {"page_key": "Concept_FailRecoverable"},
                }
            ],
        }
    )

    original_retire = db_store.retire_projection_runtime_pending

    def fail_retire(*args, **kwargs):
        raise RuntimeError("simulated-retirement-crash")

    monkeypatch.setattr(db_store, "retire_projection_runtime_pending", fail_retire)
    pending = db_store.get_projection_runtime_v9()
    with pytest.raises(RuntimeError, match="simulated-retirement-crash"):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert db_store.get_projection_runtime_v9() == pending
    monkeypatch.setattr(db_store, "retire_projection_runtime_pending", original_retire)
    tool_projection.rebuild_index_projection(dry_run=False)
    assert db_store.get_projection_runtime_v9()["status"] == "ready"


def _different_pending_roots(memory: Path, *, marker="A", stale=True):
    from vector_lake import projection_format_v2 as fmt

    db_store.init_db()
    base = memory / "wiki"
    store = ProjectionStoreV2(base)

    def prepare(label):
        empty = store.empty_root_digest
        nodes = store.apply(None, sets={f"Concept_{label}": {"title": label}}).root_digest
        descriptor = store.apply(None, sets={
            "contract": fmt.ROOT_CONTRACT, "format_version": 2, "projection": "index",
            **{name: nodes if name == "nodes" else empty for name in fmt._INDEX_ROOT_COMPONENTS},
        }).root_digest
        graph = store.apply(None, sets={
            "contract": fmt.ROOT_CONTRACT, "format_version": 2,
            "projection": "claim_graph", "nodes": empty, "edges": empty, "meta": empty,
        }).root_digest
        return fmt.prepare_projection_from_roots(
            base, index_root_sha256=descriptor, claim_graph_root_sha256=graph,
            canonical_generation=indexer.canonical_runtime_generation_snapshot(),
        )

    previous = None
    if marker is not None:
        previous = prepare("A")
        fmt.publish_prepared_projection(base, previous)
    pending = prepare("B")
    current = db_store.get_projection_runtime_v9()
    with db_store.transaction() as connection:
        db_store.cas_projection_runtime_publish_pending(
            connection, expected_status=current["status"],
            expected_projection_generation=current["projection_generation"],
            projection_generation=pending.projection_generation,
            canonical_generation=pending.canonical_generation, sidecar_json=pending.sidecar_json,
        )
    if marker == "B":
        fmt._write_durable_replace(base / fmt.SIDECAR_FILENAME, pending.sidecar_json.encode())
    if stale:
        _advance_canonical("after_pending")
    a_paths = set(fmt.validate_root_closure(base, previous.sidecar)) if previous else set()
    b_paths = set(fmt.validate_root_closure(base, pending.sidecar))
    if previous:
        assert previous.index_root_sha256 != pending.index_root_sha256
        assert a_paths - b_paths and b_paths - a_paths
    return db_store.get_projection_runtime_v9(), a_paths, b_paths


def _advance_canonical(label):
    governance_store.apply_change_set({
        "change_set_id": "cs_" + label, "affected_pages": ["Concept_Test"],
        "proposed_claims": [{"claim_id": "claim:" + label, "claim_text": label,
                             "status": "Accepted", "locator": {"page_key": "Concept_Test"}}],
    })


def _physical_snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def _business_snapshot(memory):
    marker = memory / "wiki" / "projection_pair_manifest.json"
    return (
        db_store.get_projection_runtime_v9(), marker.read_bytes() if marker.exists() else None,
        [tuple(row) for row in db_store.get_connection().execute("SELECT * FROM wiki_search_index")],
    )


@pytest.mark.parametrize("marker", ["A", "B", None])
def test_pending_recovery_distinct_closures_preview_backup_gc(isolated_memory, marker):
    from vector_lake import projection_format_v2 as fmt

    pending, a_paths, b_paths = _different_pending_roots(isolated_memory, marker=marker)
    before = _physical_snapshot(isolated_memory)
    preview = tool_projection.rebuild_index_projection(dry_run=True)
    assert "[DRY RUN]" in preview
    assert _physical_snapshot(isolated_memory) == before
    assert db_store.get_projection_runtime_v9() == pending
    if marker is None:
        assert not (isolated_memory / "wiki" / fmt.SIDECAR_FILENAME).exists()
        assert pending["previous_sidecar"] is None
    with pytest.raises(fmt.ProjectionV2ContractError, match="pending_canonical_generation_stale"):
        indexer.generate_index()
    assert db_store.get_projection_runtime_v9() == pending
    result = tool_projection.rebuild_index_projection(dry_run=False)
    backup = Path(result.split("backup=", 1)[1])
    manifest, inventory = tool_projection.validate_maintenance_backup_v4(backup / "manifest.json")
    receipt = json.loads((backup / "projection_recovery.json").read_text())
    assert receipt["predecessor"] == pending
    assert receipt["reason"] == "pending_canonical_generation_stale"
    assert receipt["successor"]["status"] == "rebuild_required"
    assert receipt["database_sha256"] == _raw_sha256(backup / "vector_lake.db")
    assert receipt["marker_sha256"] == manifest["artifact_sha256"].get(fmt.SIDECAR_FILENAME)
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is False
    assert manifest["canonical_projection_consistency"]["status"] == "unverifiable"
    assert inventory is not None
    assert {p.stem for p in a_paths | b_paths} <= {item["sha256"] for item in inventory["objects"]}
    for path in a_paths | b_paths:
        relative = tool_projection._projection_gc_object_relative(path)
        assert (backup / relative).read_bytes() == path.read_bytes()
    restore = restore_snapshot.preview_restore_snapshot(backup / "manifest.json")
    assert restore["can_apply"] is False
    _advance_canonical("later_publication")
    indexer.generate_index()
    assert db_store.get_projection_runtime_v9()["previous_sidecar"] is None
    old = time.time() - 10 * 86_400
    for path in a_paths | b_paths:
        os.utime(path, (old, old))
    gc = tool_projection.projection_object_gc(dry_run=True, retention_days=7, limit=1000)
    assert gc["can_apply"] is True, gc
    assert not ({p.stem for p in a_paths | b_paths} & {item["sha256"] for item in gc["candidates"]})


@pytest.mark.parametrize("failure", ["receipt_write", "object_copy", "retirement"])
def test_pending_recovery_persistence_failure_preserves_business_state(isolated_memory, monkeypatch, failure):
    pending, a_paths, b_paths = _different_pending_roots(isolated_memory)
    before = _business_snapshot(isolated_memory)
    if failure == "receipt_write":
        original = tool_projection._write_manifest_and_sync

        def fail(path, payload):
            if path.name == "projection_recovery.json":
                raise OSError("receipt-durability-failed")
            return original(path, payload)

        monkeypatch.setattr(tool_projection, "_write_manifest_and_sync", fail)
    elif failure == "object_copy":
        original = tool_projection.shutil.copyfile
        unique = next(iter(b_paths - a_paths))

        def fail(source, target, *args, **kwargs):
            if Path(source) == unique:
                raise OSError("pending-object-copy-failed")
            return original(source, target, *args, **kwargs)

        monkeypatch.setattr(tool_projection.shutil, "copyfile", fail)
    else:
        def fail(*args, **kwargs):
            raise OSError("retirement-transaction-failed")

        monkeypatch.setattr(db_store, "retire_projection_runtime_pending", fail)
    with pytest.raises(OSError, match="failed"):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert _business_snapshot(isolated_memory) == before
    assert db_store.get_projection_runtime_v9() == pending


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_previous_unique_object_errors_are_not_empty_preview(isolated_memory, damage):
    pending, a_paths, b_paths = _different_pending_roots(isolated_memory, marker="B")
    path = next(iter(a_paths - b_paths))
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    before = _physical_snapshot(isolated_memory)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        tool_projection.rebuild_index_projection(dry_run=True)
    assert _physical_snapshot(isolated_memory) == before
    assert db_store.get_projection_runtime_v9() == pending
    with pytest.raises((OSError, RuntimeError, ValueError)):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert db_store.get_projection_runtime_v9() == pending


def test_preview_materialization_error_propagates(isolated_memory, monkeypatch):
    pending, _a, _b = _different_pending_roots(isolated_memory)
    before = _physical_snapshot(isolated_memory)

    def fail(*args, **kwargs):
        raise OSError("previous-materialization-failed")

    monkeypatch.setattr(tool_projection, "materialize_index", fail)
    with pytest.raises(OSError, match="previous-materialization-failed"):
        tool_projection.rebuild_index_projection(dry_run=True)
    assert _physical_snapshot(isolated_memory) == before
    assert db_store.get_projection_runtime_v9() == pending


def test_same_generation_different_roots_marker_io_failure_preserves_pending(isolated_memory, monkeypatch):
    from vector_lake import projection_format_v2 as fmt

    pending, _a, _b = _different_pending_roots(isolated_memory, stale=False)
    before = _business_snapshot(isolated_memory)
    original = fmt._write_durable_replace

    def fail(path, payload):
        if path.name == fmt.SIDECAR_FILENAME:
            raise OSError("marker-replace-failed")
        return original(path, payload)

    monkeypatch.setattr(fmt, "_write_durable_replace", fail)
    with pytest.raises(OSError, match="marker-replace-failed"):
        fmt.recover_pending_publish(isolated_memory / "wiki")
    assert _business_snapshot(isolated_memory) == before
    monkeypatch.setattr(fmt, "_write_durable_replace", original)
    assert fmt.recover_pending_publish(isolated_memory / "wiki") is True
    assert db_store.get_projection_runtime_v9()["projection_generation"] == pending["projection_generation"]
    assert "Concept_B" in fmt.load_committed_index(isolated_memory / "wiki")["nodes"]


def test_retirement_canonical_race_keeps_full_intent(isolated_memory):
    pending, _a, _b = _different_pending_roots(isolated_memory)
    backup = Path(tool_projection.create_maintenance_backup("race"))
    _advance_canonical("racing_writer")
    with pytest.raises(RuntimeError, match="canonical_generation_changed_during_retirement"):
        tool_projection._retire_pending_projection_from_backup(backup)
    assert db_store.get_projection_runtime_v9() == pending


@pytest.mark.parametrize("damage", ["receipt", "object"])
def test_retirement_revalidates_durable_evidence(isolated_memory, damage):
    pending, a_paths, b_paths = _different_pending_roots(isolated_memory)
    backup = Path(tool_projection.create_maintenance_backup("damaged"))
    if damage == "receipt":
        path = backup / "projection_recovery.json"
    else:
        path = backup / tool_projection._projection_gc_object_relative(next(iter(b_paths - a_paths)))
    path.write_bytes(b"damaged")
    with pytest.raises((RuntimeError, ValueError), match="mismatch"):
        tool_projection._retire_pending_projection_from_backup(backup)
    assert db_store.get_projection_runtime_v9() == pending


def test_retired_rebuild_interruption_resumes_same_receipt(isolated_memory, monkeypatch):
    pending, _a, _b = _different_pending_roots(isolated_memory)
    original = indexer.generate_index

    def fail(*args, **kwargs):
        raise RuntimeError("rebuild-process-interrupted")

    monkeypatch.setattr(indexer, "generate_index", fail)
    with pytest.raises(RuntimeError, match="rebuild-process-interrupted"):
        tool_projection.rebuild_index_projection(dry_run=False)
    runtime = db_store.get_projection_runtime_v9()
    assert runtime["status"] == "rebuild_required"
    backup = tool_projection._projection_recovery_resume_backup()
    assert backup is not None
    receipt = json.loads((backup / "projection_recovery.json").read_text())
    assert receipt["predecessor"] == pending and receipt["successor"] == runtime
    assert tool_projection._retire_pending_projection_from_backup(backup) is True
    assert db_store.get_projection_runtime_v9() == runtime
    monkeypatch.setattr(indexer, "generate_index", original)
    result = tool_projection.rebuild_index_projection(dry_run=False)
    assert result.endswith(f"backup={backup}")
    assert db_store.get_projection_runtime_v9()["status"] == "ready"


def _completion_backup(memory):
    return next((memory / "wiki" / ".meta" / "backups").glob("index_rebuild_*"))


def _completion_files(backup):
    binding, _receipt = tool_projection._completion_binding(backup)
    return tool_projection._completion_paths(binding)


def test_completion_real_convergence_historical_and_readonly(isolated_memory):
    pending, _a, _b = _different_pending_roots(isolated_memory)
    result = tool_projection.rebuild_index_projection(dry_run=False)
    backup = Path(result.split("backup=", 1)[1])
    frozen = _physical_snapshot(backup)
    proof = tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")
    runtime = db_store.get_projection_runtime_v9()
    assert proof["final_sidecar"] == runtime["sidecar"]
    assert proof["final_sidecar_sha256"] == runtime["sidecar_sha256"]
    assert proof["final_sidecar"]["index_root_sha256"] != pending["sidecar"]["index_root_sha256"]
    assert proof["binding"]["manifest_sha256"] == _raw_sha256(backup / "manifest.json")
    assert proof["binding"]["retirement_sha256"] == _raw_sha256(backup / "projection_recovery.json")
    assert len(proof["intent_fingerprints"]) == 2
    files = _completion_files(backup)
    physical = _physical_snapshot(files["completed"].parent)
    assert tool_projection.rebuild_index_projection(dry_run=False).endswith(f"backup={backup}")
    assert _physical_snapshot(files["completed"].parent) == physical
    assert _physical_snapshot(backup) == frozen
    before = _physical_snapshot(isolated_memory)
    tool_projection.rebuild_index_projection(dry_run=True)
    assert _physical_snapshot(isolated_memory) == before
    _advance_canonical("after_completion")
    fresh = tool_projection.rebuild_index_projection(dry_run=False)
    assert not fresh.endswith(f"backup={backup}")
    assert "completion_proof=" not in fresh
    assert indexer.projection_pair_matches_current_generation()
    assert _physical_snapshot(files["completed"].parent) == physical
    historical = tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")
    assert historical == proof and historical["historically_valid"]
    assert historical["proves_current_readiness"] is False
    assert _physical_snapshot(backup) == frozen


@pytest.mark.parametrize("stage", ["rebuild", "topology", "completed"])
def test_completion_persistence_failure_and_exact_replay(isolated_memory, monkeypatch, stage):
    _different_pending_roots(isolated_memory)
    original = tool_projection._write_completion_record
    captured = {}

    def fail(path, payload):
        if path.name.endswith(f".{stage}.json"):
            captured["runtime"] = db_store.get_projection_runtime_v9()
            raise OSError("completion-persist-failed")
        return original(path, payload)

    monkeypatch.setattr(tool_projection, "_write_completion_record", fail)
    with pytest.raises((OSError, RuntimeError), match="completion-persist-failed"):
        tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(isolated_memory)
    files = _completion_files(backup)
    assert not files["completed"].exists()
    assert db_store.get_projection_runtime_v9() == captured["runtime"]
    if stage == "rebuild":
        assert captured["runtime"]["status"] == "rebuild_required"
    else:
        assert captured["runtime"]["status"] == "ready"
    frozen = _physical_snapshot(backup)
    monkeypatch.setattr(tool_projection, "_write_completion_record", original)
    result = tool_projection.rebuild_index_projection(dry_run=False)
    assert result.endswith(f"backup={backup}")
    assert _physical_snapshot(backup) == frozen
    assert len(list(files["completed"].parent.iterdir())) == 3
    assert tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")["historically_valid"]


@pytest.mark.parametrize("stage", ["rebuild", "topology"])
def test_completion_prepared_before_publish_interruption_replays(isolated_memory, monkeypatch, stage):
    _different_pending_roots(isolated_memory)
    original = indexer.publish_prepared_projection
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == (1 if stage == "rebuild" else 2):
            raise OSError("before-publication-interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(indexer, "publish_prepared_projection", fail)
    with pytest.raises(OSError, match="before-publication-interruption"):
        tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(isolated_memory)
    files = _completion_files(backup)
    intent = files[stage].read_bytes()
    monkeypatch.setattr(indexer, "publish_prepared_projection", original)
    tool_projection.rebuild_index_projection(dry_run=False)
    assert files[stage].read_bytes() == intent
    assert tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")["historically_valid"]


@pytest.mark.parametrize("drift", ["canonical", "root"])
def test_completion_rejects_drift_and_unrelated_ready(isolated_memory, monkeypatch, drift):
    from vector_lake import projection_format_v2 as fmt

    _different_pending_roots(isolated_memory)
    original = tool_projection._record_rebuild_completion

    def fail(backup, snapshot):
        if drift == "canonical":
            _advance_canonical("before_proof")
        else:
            current = db_store.get_projection_runtime_v9()["sidecar"]
            changed = dict(current)
            changed["published_at_utc"] = "2026-09-05T00:00:00+00:00"
            fmt._write_durable_replace(isolated_memory / "wiki" / fmt.SIDECAR_FILENAME,
                                      tool_projection._completion_json(changed))
        return original(backup, snapshot)

    monkeypatch.setattr(tool_projection, "_record_rebuild_completion", fail)
    with pytest.raises(RuntimeError, match="committed-but-completion-unproven"):
        tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(isolated_memory)
    assert not _completion_files(backup)["completed"].exists()
    monkeypatch.setattr(tool_projection, "_record_rebuild_completion", original)
    _advance_canonical("unrelated_ready")
    indexer.generate_index()
    with pytest.raises(ValueError, match="replay_successor_mismatch"):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert not _completion_files(backup)["completed"].exists()


@pytest.mark.parametrize("damage", ["binding", "chain", "extra", "digest", "oversize"])
def test_completion_tampering_rejected(isolated_memory, damage):
    _different_pending_roots(isolated_memory)
    tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(isolated_memory)
    path = _completion_files(backup)["completed"]
    value = json.loads(path.read_bytes())
    if damage == "binding":
        value["binding"]["retirement_sha256"] = "0" * 64
    elif damage == "chain":
        value["intent_fingerprints"] = []
    elif damage == "extra":
        value["unpin"] = True
    elif damage == "digest":
        value["final_sidecar_sha256"] = "0" * 64
    else:
        path.write_bytes(b" " * (tool_projection._PROJECTION_COMPLETION_MAX_BYTES + 1))
    if damage != "oversize":
        unsigned = {k: v for k, v in value.items() if k != "fingerprint"}
        value["fingerprint"] = hashlib.sha256(tool_projection._completion_json(unsigned)).hexdigest()
        path.write_bytes(tool_projection._completion_json(value))
    with pytest.raises(ValueError, match="projection_completion"):
        tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")


def test_retirement_alone_is_not_completion(isolated_memory):
    _different_pending_roots(isolated_memory)
    backup = Path(tool_projection.create_maintenance_backup("retirement_only"))
    assert tool_projection._retire_pending_projection_from_backup(backup)
    with pytest.raises(ValueError, match="intent_missing"):
        tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")
    assert not _completion_files(backup)["completed"].exists()


@pytest.mark.parametrize("damage", ["corpus", "projection_generation", "canonical_generation"])
def test_completion_fts_corruption_on_replay_stays_pinned(isolated_memory, monkeypatch, damage):
    from vector_lake import tool_backup_retention

    _different_pending_roots(isolated_memory)
    original = tool_projection._record_rebuild_completion

    def fail(*args):
        raise OSError("proof-interrupted")

    monkeypatch.setattr(tool_projection, "_record_rebuild_completion", fail)
    with pytest.raises(RuntimeError, match="committed-but-completion-unproven"):
        tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(isolated_memory)
    frozen = _physical_snapshot(backup)
    corpus = db_store.inspect_search_projection_corpus()
    state = db_store.get_search_projection_state()
    with db_store.transaction() as connection:
        if damage == "corpus":
            connection.execute("INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
                               ("extra", "extra", "extra", "extra"))
        elif damage == "projection_generation":
            connection.execute("UPDATE search_projection_state_v8 SET projection_generation = ? WHERE singleton = 1",
                               ("another-generation",))
        else:
            changed = {key: value + 1 for key, value in state["canonical_generation"].items()}
            connection.execute("UPDATE search_projection_state_v8 SET canonical_generation_json = ? WHERE singleton = 1",
                               (json.dumps(changed),))
    if damage != "corpus":
        assert db_store.inspect_search_projection_corpus() == corpus
        changed_state = db_store.get_search_projection_state()
        assert changed_state["expected_row_count"] == state["expected_row_count"]
        assert changed_state["expected_corpus_sha256"] == state["expected_corpus_sha256"]
        other_binding = "canonical_generation" if damage == "projection_generation" else "projection_generation"
        assert changed_state[other_binding] == state[other_binding]
        assert db_store.verify_search_projection_integrity()["status"] == "ready"
    monkeypatch.setattr(tool_projection, "_record_rebuild_completion", original)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="completion_fts_unverified"):
            tool_projection.rebuild_index_projection(dry_run=False)
        assert not _completion_files(backup)["completed"].exists()
        assert len(list(_completion_files(backup)["rebuild"].parent.iterdir())) == 2
    plan = json.loads(tool_backup_retention.backup_retention_maintenance(dry_run=True))
    assert backup.name in {item["name"] for item in plan["protected"]}
    assert _physical_snapshot(backup) == frozen


def test_completion_does_not_unpin_or_relax_backup_extras(isolated_memory):
    from vector_lake import tool_backup_retention

    _different_pending_roots(isolated_memory)
    tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(isolated_memory)
    before = _physical_snapshot(isolated_memory)
    plan = json.loads(tool_backup_retention.backup_retention_maintenance(dry_run=True))
    assert backup.name in {item["name"] for item in plan["protected"]}
    assert _physical_snapshot(isolated_memory) == before
    (backup / "completion.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown_or_missing"):
        tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")


def test_completion_corrupt_final_closure_never_records(isolated_memory, monkeypatch):
    _different_pending_roots(isolated_memory)
    original = tool_projection._record_rebuild_completion

    def corrupt(backup, snapshot):
        sidecar = db_store.get_projection_runtime_v9()["sidecar"]
        path = ProjectionStoreV2(isolated_memory / "wiki").object_path(sidecar["index_root_sha256"])
        path.write_bytes(b"corrupt")
        return original(backup, snapshot)

    monkeypatch.setattr(tool_projection, "_record_rebuild_completion", corrupt)
    with pytest.raises(RuntimeError, match="committed-but-completion-unproven"):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert not _completion_files(_completion_backup(isolated_memory))["completed"].exists()


def _interrupt_topology_completion(memory, monkeypatch):
    _different_pending_roots(memory)
    original = indexer.publish_prepared_projection
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("topology-intent-before-publish")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(indexer, "publish_prepared_projection", interrupt)
        with pytest.raises(OSError, match="topology-intent-before-publish"):
            tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(memory)
    files = _completion_files(backup)
    assert files["topology"].is_file() and not files["completed"].exists()
    return backup, files


def test_projection_object_gc_unfinished_topology_intent_preserves_exact_replay(isolated_memory, monkeypatch):
    backup, files = _interrupt_topology_completion(isolated_memory, monkeypatch)
    intent = files["topology"].read_bytes()
    sidecar = json.loads(intent)["sidecar"]
    prepared = tool_projection.validate_root_closure(isolated_memory / "wiki", sidecar)
    protected, _binding, _issues = tool_projection._projection_gc_protection()
    unique = {path for path in prepared if path.stem not in protected}
    assert unique, "fixture must expose prepared roots outside live/backup protection"
    old = time.time() - 10 * 86_400
    for path in unique:
        os.utime(path, (old, old))
    frozen = _physical_snapshot(backup)
    objects = _physical_snapshot(isolated_memory / "wiki" / ".projection-store")
    preview = tool_projection.projection_object_gc(retention_days=7, limit=1000)
    assert unique.intersection({isolated_memory / "wiki" / item["relative_path"]
                                for item in preview["candidates"]})
    assert preview["can_apply"] is False
    assert "projection_completion_unfinished" in preview["issues"]
    with pytest.raises(RuntimeError, match="cannot apply"):
        tool_projection.projection_object_gc(dry_run=False, retention_days=7, limit=1000,
                                            confirmation=preview["fingerprint"])
    assert _physical_snapshot(isolated_memory / "wiki" / ".projection-store") == objects
    assert _physical_snapshot(backup) == frozen
    assert not (isolated_memory / "wiki" / ".meta" / "projection-object-gc-receipts").exists()
    tool_projection.rebuild_index_projection(dry_run=False)
    assert files["topology"].read_bytes() == intent
    assert tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")["historically_valid"]
    assert _physical_snapshot(backup) == frozen



def test_projection_object_gc_completed_history_does_not_require_live_roots(isolated_memory):
    _different_pending_roots(isolated_memory)
    tool_projection.rebuild_index_projection(dry_run=False)
    backup = _completion_backup(isolated_memory)
    proof = tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")
    files = _completion_files(backup)
    records = _physical_snapshot(files["completed"].parent)
    frozen = _physical_snapshot(backup)
    _advance_canonical("gc_after_completion")
    # Ordinary publication avoids creating another backup that correctly pins
    # the old final roots; the separate historical-rebuild test uses maintenance.
    indexer.generate_index()
    indexer.refresh_graph_topology_if_dirty()
    store = ProjectionStoreV2(isolated_memory / "wiki")
    old = time.time() - 10 * 86_400
    for path in store.objects_dir.glob("*/*.json"):
        os.utime(path, (old, old))
    preview = tool_projection.projection_object_gc(retention_days=7, limit=1000)
    assert preview["can_apply"] is True, preview["issues"]
    historical_root = proof["final_sidecar"]["index_root_sha256"]
    assert historical_root in {item["sha256"] for item in preview["candidates"]}
    applied = tool_projection.projection_object_gc(dry_run=False, retention_days=7, limit=1000,
                                                   confirmation=preview["fingerprint"])
    assert applied["deleted_objects"] > 0
    assert not store.object_path(historical_root).exists()
    assert tool_projection.validate_projection_rebuild_completion(backup / "manifest.json") == proof
    assert tool_projection.projection_object_gc()["can_apply"] is True
    assert _physical_snapshot(files["completed"].parent) == records
    assert _physical_snapshot(backup) == frozen


@pytest.mark.parametrize("damage", ["malformed", "missing_rebuild", "missing_topology",
                                   "missing_backup", "unknown", "scan_failure", "scan_limit"])
def test_projection_object_gc_completion_evidence_fails_closed(isolated_memory, monkeypatch, damage):
    from contextlib import nullcontext
    from types import SimpleNamespace

    backup, files = _interrupt_topology_completion(isolated_memory, monkeypatch)
    if damage == "missing_topology":
        tool_projection.rebuild_index_projection(dry_run=False)
        files["topology"].unlink()
    elif damage == "malformed":
        files["topology"].write_bytes(b"{")
    elif damage == "missing_rebuild":
        files["rebuild"].unlink()
    elif damage == "missing_backup":
        (backup / "manifest.json").unlink()
    elif damage == "unknown":
        (files["rebuild"].parent / "unknown.json").write_bytes(b"{}")
    elif damage in {"scan_failure", "scan_limit"}:
        original = os.scandir

        def scan(path):
            if Path(path) == files["rebuild"].parent:
                if damage == "scan_failure":
                    raise OSError("completion-scan-failed")
                entry = SimpleNamespace(name=files["rebuild"].name, path=str(files["rebuild"]))
                return nullcontext(iter([entry] * 30_001))
            return original(path)

        monkeypatch.setattr(tool_projection.os, "scandir", scan)
    objects = _physical_snapshot(isolated_memory / "wiki" / ".projection-store")
    frozen = _physical_snapshot(backup)
    preview = tool_projection.projection_object_gc(retention_days=7, limit=1000)
    assert preview["can_apply"] is False
    assert any(issue.startswith("projection_completion_evidence_invalid:") for issue in preview["issues"])
    with pytest.raises(RuntimeError, match="cannot apply"):
        tool_projection.projection_object_gc(dry_run=False, retention_days=7, limit=1000,
                                            confirmation=preview["fingerprint"])
    assert _physical_snapshot(isolated_memory / "wiki" / ".projection-store") == objects
    assert _physical_snapshot(backup) == frozen
    assert not (isolated_memory / "wiki" / ".meta" / "projection-object-gc-receipts").exists()


@pytest.mark.parametrize("boundary", ["old_preview", "pending_resume", "before_delete"])
def test_projection_object_gc_new_intent_fences_every_apply_boundary(isolated_memory, monkeypatch, boundary):
    backup, files = _interrupt_topology_completion(isolated_memory, monkeypatch)
    # Stage the same valid, not-yet-published evidence after an older GC preview.
    # Only receipt evidence changes: the runtime, backups and candidate objects do not.
    records = {stage: json.loads(files[stage].read_bytes()) for stage in ("rebuild", "topology")}
    for stage in records:
        files[stage].unlink()
    store = ProjectionStoreV2(isolated_memory / "wiki")
    orphan = store.apply(None, sets={"gc-race-orphan": {"value": "discard"}})
    old = time.time() - 10 * 86_400
    for path in store.object_paths(orphan.root_digest, max_objects=128):
        os.utime(path, (old, old))
    preview = tool_projection.projection_object_gc(retention_days=7, limit=1000)
    assert preview["can_apply"] is True
    assert orphan.root_digest in {item["sha256"] for item in preview["candidates"]}
    objects = _physical_snapshot(isolated_memory / "wiki" / ".projection-store")
    frozen = _physical_snapshot(backup)

    def persist_intents():
        for stage, record in records.items():
            tool_projection._write_completion_record(files[stage],
                {key: value for key, value in record.items() if key != "fingerprint"})

    def apply():
        return tool_projection.projection_object_gc(dry_run=False, retention_days=7, limit=1000,
                                                    confirmation=preview["fingerprint"])

    if boundary == "before_delete":
        original_protection = tool_projection._projection_gc_protection
        calls = 0

        def protection():
            nonlocal calls
            calls += 1
            if calls == 3:  # Initial plan, locked plan, then the pre-delete fence.
                persist_intents()
            return original_protection()

        with monkeypatch.context() as patch:
            patch.setattr(tool_projection, "_projection_gc_protection", protection)
            with pytest.raises(RuntimeError, match="protection binding changed"):
                apply()
        assert calls == 3
    else:
        if boundary == "pending_resume":
            original_write = tool_projection.atomic_write_text

            def stop_after_pending(path, text):
                result = original_write(path, text)
                if path.name.endswith(".pending.json"):
                    raise OSError("gc-pending-persisted")
                return result

            with monkeypatch.context() as patch:
                patch.setattr(tool_projection, "atomic_write_text", stop_after_pending)
                with pytest.raises(OSError, match="gc-pending-persisted"):
                    apply()
        persist_intents()
        with pytest.raises(RuntimeError, match="fingerprint changed|protection binding changed"):
            apply()
    changed = tool_projection.projection_object_gc(retention_days=7, limit=1000)
    assert changed["fingerprint"] != preview["fingerprint"]
    assert changed["protection_binding"]["rebuild_completions"]
    assert {key: value for key, value in changed["protection_binding"].items() if key != "rebuild_completions"} == {
        key: value for key, value in preview["protection_binding"].items() if key != "rebuild_completions"}
    assert _physical_snapshot(isolated_memory / "wiki" / ".projection-store") == objects
    assert _physical_snapshot(backup) == frozen
    tool_projection.rebuild_index_projection(dry_run=False)
    assert tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")["historically_valid"]



@pytest.mark.parametrize("new_intent", [False, True])
def test_projection_object_gc_old_format_pending_resume_fences_new_intent(isolated_memory, monkeypatch, new_intent):
    backup, files = _interrupt_topology_completion(isolated_memory, monkeypatch)
    records = {stage: json.loads(files[stage].read_bytes()) for stage in ("rebuild", "topology")}
    for stage in records:
        files[stage].unlink()
    store = ProjectionStoreV2(isolated_memory / "wiki")
    old = time.time() - 10 * 86_400
    for label in ("old-pending-a", "old-pending-b"):
        orphan = store.apply(None, sets={label: {"value": label}})
        for path in store.object_paths(orphan.root_digest, max_objects=128):
            os.utime(path, (old, old))
    preview = tool_projection.projection_object_gc(retention_days=7, limit=1000)
    assert preview["can_apply"] and len(preview["candidates"]) >= 2
    assert set(preview["protection_binding"]) == {
        "live", "runtime_sidecars", "maintenance_backups", "pending_receipts"}

    def apply():
        return tool_projection.projection_object_gc(dry_run=False, retention_days=7, limit=1000,
                                                    confirmation=preview["fingerprint"])

    def interrupt(deleted, digest):
        assert deleted == 1
        raise OSError("old-format-gc-interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(tool_projection, "_TEST_PROJECTION_GC_FAULT_HOOK", interrupt)
        with pytest.raises(OSError, match="old-format-gc-interrupted"):
            apply()
    receipt_path = (isolated_memory / "wiki" / ".meta" / "projection-object-gc-receipts" /
                    (preview["fingerprint"].removeprefix("sha256:") + ".pending.json"))
    pending_bytes = receipt_path.read_bytes()
    pending = json.loads(pending_bytes)
    assert tool_projection._projection_gc_receipt_valid(pending, status="pending")
    assert pending["protection_binding"] == preview["protection_binding"]
    objects = _physical_snapshot(isolated_memory / "wiki" / ".projection-store")
    frozen = _physical_snapshot(backup)
    if new_intent:
        for stage, record in records.items():
            tool_projection._write_completion_record(files[stage],
                {key: value for key, value in record.items() if key != "fingerprint"})
        with pytest.raises(RuntimeError, match="protection binding changed"):
            apply()
        assert receipt_path.read_bytes() == pending_bytes
        assert _physical_snapshot(isolated_memory / "wiki" / ".projection-store") == objects
        assert not receipt_path.with_name(receipt_path.name.replace(".pending.", ".completed.")).exists()
        tool_projection.rebuild_index_projection(dry_run=False)
        assert tool_projection.validate_projection_rebuild_completion(backup / "manifest.json")["historically_valid"]
    else:
        resumed = apply()
        assert resumed["resumed"] is True
        assert resumed["already_missing_objects"] == 1
        assert resumed["deleted_objects"] == len(preview["candidates"]) - 1
        assert not receipt_path.exists()
    assert _physical_snapshot(backup) == frozen
