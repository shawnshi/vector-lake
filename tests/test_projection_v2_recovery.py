import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from vector_lake import db_store, indexer, restore_snapshot, tool_projection
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
    assert projection_v2_reachable_inventory()["sidecar_sha256"] == live_before[
        "sidecar_sha256"
    ]

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
