import hashlib
import json
from contextlib import closing
import os
import sqlite3
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vector_lake import db_store, indexer, tool_backup_retention
from vector_lake.tool_projection import create_maintenance_backup


def _set_age(path: Path, *, age: timedelta) -> None:
    timestamp = (datetime.now(timezone.utc) - age).timestamp()
    os.utime(path, (timestamp, timestamp))


def _set_manifest_created_at(path: Path, *, created_at: datetime) -> None:
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = created_at.isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _complete_backup(
    root: Path,
    name: str,
    *,
    age: timedelta,
    payload: bytes = b"backup",
    created_at: datetime | None = None,
    restorable: bool = False,
) -> Path:
    path = root / name
    path.mkdir()
    database_path = path / "vector_lake.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "CREATE TABLE runtime_generations "
            "(surface TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
        )
        connection.execute("CREATE TABLE fixture_payload (value BLOB NOT NULL)")
        connection.execute("INSERT INTO fixture_payload (value) VALUES (?)", (payload,))
        connection.commit()
    artifacts = {"vector_lake.db": database_path.read_bytes()}
    if restorable:
        artifacts["index.json"] = b"{}"
        artifacts["claim_graph.json"] = b"{}"
        (path / "index.json").write_bytes(artifacts["index.json"])
        (path / "claim_graph.json").write_bytes(artifacts["claim_graph.json"])
    copied = list(artifacts)
    projection_generation = "fixture-generation" if restorable else None
    projection_binding = {"status": "verified"} if restorable else None
    consistency_status = "verified" if restorable else "not_applicable"
    consistency_reason = (
        "runtime-generations-match" if restorable else "projection-pair-absent"
    )
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "created_at": (
                    created_at or datetime.now(timezone.utc) - age
                ).isoformat(),
                "label": name,
                "copied": copied,
                "artifact_sha256": {
                    artifact_name: hashlib.sha256(content).hexdigest()
                    for artifact_name, content in artifacts.items()
                },
                "database_runtime_generations": {},
                "database_runtime_generation_error": None,
                "projection_generation": projection_generation,
                "projection_canonical_generation": projection_binding,
                "canonical_projection_consistency": {
                    "status": consistency_status,
                    "reason": consistency_reason,
                    "verification_scope": "tracked-canonical-projection-surfaces",
                    "covered_surfaces": [],
                },
                "restorable_as_consistent_canonical_projection_snapshot": restorable,
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    _set_age(path, age=age)
    return path


def _stage(root: Path, name: str, *, age: timedelta) -> Path:
    path = root / name
    path.mkdir()
    (path / "partial.bin").write_bytes(b"partial")
    _set_age(path, age=age)
    return path


@pytest.fixture
def backup_root(tmp_path, monkeypatch):
    meta = tmp_path / ".meta"
    root = meta / "backups"
    root.mkdir(parents=True)
    monkeypatch.setattr(tool_backup_retention, "peek_meta_dir", lambda: meta)
    return root


@pytest.mark.parametrize(
    "name",
    ["keep_latest", "min_age_days", "stage_ttl_hours"],
)
def test_backup_retention_rejects_nonpositive_bounds(backup_root, name):
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 1,
        "stage_ttl_hours": 1,
    }
    kwargs[name] = 0

    with pytest.raises(ValueError, match=name):
        tool_backup_retention.backup_retention_maintenance(**kwargs)


@pytest.mark.parametrize("bad_value", [True, 1.5, "1"])
@pytest.mark.parametrize(
    "name",
    ["keep_latest", "min_age_days", "stage_ttl_hours"],
)
def test_backup_retention_rejects_non_integer_bounds(
    backup_root,
    name,
    bad_value,
):
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 1,
        "stage_ttl_hours": 1,
    }
    kwargs[name] = bad_value

    with pytest.raises(TypeError, match=name):
        tool_backup_retention.backup_retention_maintenance(**kwargs)


def test_current_maintenance_backup_is_retention_eligible(isolated_memory):
    db_store.init_db()
    older = Path(create_maintenance_backup("retention_older"))
    newer = Path(create_maintenance_backup("retention_newer"))
    current = datetime.now(timezone.utc)
    _set_manifest_created_at(older, created_at=current - timedelta(days=90))
    _set_manifest_created_at(newer, created_at=current - timedelta(days=60))
    _set_age(older, age=timedelta(days=90))
    _set_age(newer, age=timedelta(days=60))

    for backup in (older, newer):
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        assert {entry.name for entry in backup.iterdir()} == (
            set(manifest["copied"]) | {"manifest.json"}
        )
        assert not (backup / "vector_lake.db-wal").exists()
        assert not (backup / "vector_lake.db-shm").exists()

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert {(item["name"], item["type"]) for item in result["candidates"]} == {
        (older.name, "complete_backup")
    }
    assert {item["name"] for item in result["protected"]} == {newer.name}
    assert result["ignored"] == []


def test_backup_retention_preview_selects_only_expired_direct_candidates(
    backup_root,
):
    oldest = _complete_backup(
        backup_root,
        "maintenance_oldest",
        age=timedelta(days=90),
        payload=b"oldest",
    )
    retained = _complete_backup(
        backup_root,
        "maintenance_retained",
        age=timedelta(days=60),
        payload=b"retained",
    )
    young = _complete_backup(
        backup_root,
        "maintenance_young",
        age=timedelta(hours=2),
        payload=b"young",
    )
    expired_stage = _stage(
        backup_root,
        ".maintenance_20260401T000000000000Z.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp",
        age=timedelta(hours=72),
    )
    fresh_stage = _stage(
        backup_root,
        ".maintenance_20260727T000000000000Z.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.tmp",
        age=timedelta(hours=1),
    )
    incomplete = backup_root / "maintenance_incomplete"
    incomplete.mkdir()
    (incomplete / "manifest.json").write_text(
        json.dumps({"complete": False}),
        encoding="utf-8",
    )
    impostor = backup_root / "maintenance_impostor"
    impostor.mkdir()
    (impostor / "manifest.json").write_text(
        json.dumps({"complete": True}),
        encoding="utf-8",
    )
    _set_age(impostor, age=timedelta(days=90))
    unrelated_stage = _stage(
        backup_root,
        ".notes.tmp",
        age=timedelta(days=10),
    )
    uuid_shaped_unrelated_stage = _stage(
        backup_root,
        ".notes.dddddddddddddddddddddddddddddddd.tmp",
        age=timedelta(days=10),
    )
    nested_stage = incomplete / ".nested.tmp"
    nested_stage.mkdir()
    _set_age(nested_stage, age=timedelta(days=10))

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=2,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert result["dry_run"] is True
    assert result["applied"] is False
    assert {(item["name"], item["type"]) for item in result["candidates"]} == {
        (oldest.name, "complete_backup"),
        (expired_stage.name, "expired_staging"),
    }
    assert result["candidate_count"] == 2
    assert result["candidate_bytes"] == sum(
        item["size"] for item in result["candidates"]
    )
    assert result["fingerprint"].startswith("sha256:")
    assert {item["name"] for item in result["protected"]} == {
        retained.name,
        young.name,
    }
    assert fresh_stage.is_dir()
    assert nested_stage.is_dir()
    assert unrelated_stage.is_dir()
    assert uuid_shaped_unrelated_stage.is_dir()
    assert impostor.is_dir()


def test_backup_retention_always_preserves_latest_restorable_backup(
    backup_root,
    monkeypatch,
):
    monkeypatch.setattr(
        tool_backup_retention,
        "_verify_restorable_backup_snapshot",
        lambda *_args, **_kwargs: None,
    )
    oldest_unverifiable = _complete_backup(
        backup_root,
        "oldest_unverifiable",
        age=timedelta(days=120),
        created_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
    )
    latest_restorable = _complete_backup(
        backup_root,
        "latest_restorable",
        age=timedelta(days=90),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        restorable=True,
    )
    newest_unverifiable = _complete_backup(
        backup_root,
        "newest_unverifiable",
        age=timedelta(days=60),
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert {item["name"] for item in result["candidates"]} == {oldest_unverifiable.name}
    protected = {item["name"]: item for item in result["protected"]}
    assert set(protected) == {latest_restorable.name, newest_unverifiable.name}
    assert "latest_restorable" in protected[latest_restorable.name]["reason"]
    assert protected[latest_restorable.name]["restorable"] is True
    assert protected[latest_restorable.name]["consistency_status"] == "verified"
    assert protected[newest_unverifiable.name]["restorable"] is False


def _create_verified_backup(
    label: str,
    *,
    created_at: datetime,
    age: timedelta,
) -> Path:
    path = Path(create_maintenance_backup(label))
    _set_manifest_created_at(path, created_at=created_at)
    _set_age(path, age=age)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["restorable_as_consistent_canonical_projection_snapshot"] is True
    return path


def test_restorable_verification_releases_each_projection_root_before_next_decode(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    indexer.generate_index()
    path = Path(create_maintenance_backup("projection_root_lifetime"))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    real_loads = json.loads
    root_refs = []

    class TrackingProjectionRoot(dict):
        pass

    def tracking_loads(payload):
        decoded = real_loads(payload)
        if root_refs:
            assert root_refs[-1]() is None
        root = TrackingProjectionRoot(decoded)
        root_refs.append(weakref.ref(root))
        return root

    class TrackingJson:
        loads = staticmethod(tracking_loads)
        JSONDecodeError = json.JSONDecodeError

    monkeypatch.setattr(tool_backup_retention, "json", TrackingJson)

    tool_backup_retention._verify_restorable_backup_snapshot(path, manifest)

    assert len(root_refs) == 3
    assert all(reference() is None for reference in root_refs)


def test_backup_retention_falls_back_to_older_verified_guard_after_hash_failure(
    isolated_memory,
):
    db_store.init_db()
    indexer.generate_index()
    current = datetime.now(timezone.utc)
    older = _create_verified_backup(
        "verified_guard_older",
        created_at=current - timedelta(days=90),
        age=timedelta(days=90),
    )
    newer = _create_verified_backup(
        "corrupt_claim_newer",
        created_at=current - timedelta(days=60),
        age=timedelta(days=60),
    )
    with (newer / "vector_lake.db").open("ab") as handle:
        handle.write(b"corrupt")

    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))

    assert preview["candidate_count"] == 0
    assert preview["restorable_guard"]["name"] == older.name
    failures = preview["restorable_verification_failures"]
    assert [item["name"] for item in failures] == [newer.name]
    assert "backup_artifact_hash_mismatch" in failures[0]["verification_error"]
    protected = {item["name"]: item for item in preview["protected"]}
    assert "latest_restorable" in protected[older.name]["reason"]
    assert "restorable_verification_failed" in protected[newer.name]["reason"]

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )
    )
    assert result["deleted_count"] == 0
    assert older.is_dir()
    assert newer.is_dir()


def test_backup_retention_rejects_schema_invalid_claim_with_matching_hash(
    isolated_memory,
):
    db_store.init_db()
    indexer.generate_index()
    current = datetime.now(timezone.utc)
    older = _create_verified_backup(
        "schema_guard_older",
        created_at=current - timedelta(days=90),
        age=timedelta(days=90),
    )
    newer = _create_verified_backup(
        "schema_invalid_newer",
        created_at=current - timedelta(days=60),
        age=timedelta(days=60),
    )
    database_path = newer / "vector_lake.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.commit()
    manifest_path = newer / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"]["vector_lake.db"] = hashlib.sha256(
        database_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _set_age(newer, age=timedelta(days=60))

    preview = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert preview["candidate_count"] == 0
    assert preview["restorable_guard"]["name"] == older.name
    failures = preview["restorable_verification_failures"]
    assert [item["name"] for item in failures] == [newer.name]
    assert "backup_schema_invalid" in failures[0]["verification_error"]
    assert older.is_dir()
    assert newer.is_dir()


def test_backup_retention_revalidates_guard_artifacts_without_repeating_semantics(
    backup_root,
    monkeypatch,
):
    candidate = _complete_backup(
        backup_root,
        "old_candidate",
        age=timedelta(days=120),
        created_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
    )
    guard = _complete_backup(
        backup_root,
        "verified_guard",
        age=timedelta(days=90),
        created_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
        restorable=True,
    )
    newest = _complete_backup(
        backup_root,
        "newest_nonrestorable",
        age=timedelta(days=60),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    semantic_calls = {"count": 0}
    guard_artifact_calls = {"count": 0}
    real_verify_artifacts = tool_backup_retention._verify_complete_backup_artifacts

    def record_semantic_verification(*_args, **_kwargs):
        semantic_calls["count"] += 1

    def fail_guard_artifact_verification(path, manifest):
        if path == guard:
            guard_artifact_calls["count"] += 1
            raise RuntimeError("injected guard failure")
        return real_verify_artifacts(path, manifest)

    monkeypatch.setattr(
        tool_backup_retention,
        "_verify_restorable_backup_snapshot",
        record_semantic_verification,
    )
    monkeypatch.setattr(
        tool_backup_retention,
        "_verify_complete_backup_artifacts",
        fail_guard_artifact_verification,
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))
    assert {item["name"] for item in preview["candidates"]} == {candidate.name}

    with pytest.raises(RuntimeError, match="restorable_guard_verification_failed"):
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )

    assert semantic_calls["count"] == 3
    assert guard_artifact_calls["count"] == 1
    assert candidate.is_dir()
    assert guard.is_dir()
    assert newest.is_dir()
    assert not list(backup_root.glob(".retention-*.tombstone"))


def test_backup_retention_stops_when_guard_changes_after_apply_preflight(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    indexer.generate_index()
    current = datetime.now(timezone.utc)
    oldest = _create_verified_backup(
        "toc_oldest",
        created_at=current - timedelta(days=120),
        age=timedelta(days=120),
    )
    older = _create_verified_backup(
        "toc_older",
        created_at=current - timedelta(days=90),
        age=timedelta(days=90),
    )
    guard = _create_verified_backup(
        "toc_guard",
        created_at=current - timedelta(days=60),
        age=timedelta(days=60),
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))
    candidate_names = {oldest.name, older.name}
    assert {item["name"] for item in preview["candidates"]} == candidate_names
    assert preview["restorable_guard"]["name"] == guard.name

    original_revalidate = tool_backup_retention._revalidate_candidate
    changed = {"done": False}

    def mutate_guard_during_candidate_preflight(root, item, **call_kwargs):
        result = original_revalidate(root, item, **call_kwargs)
        if item["name"] in candidate_names and not changed["done"]:
            database_path = guard / "vector_lake.db"
            with database_path.open("r+b") as handle:
                first = handle.read(1)
                handle.seek(0)
                handle.write(bytes([first[0] ^ 0xFF]))
            changed["done"] = True
        return result

    monkeypatch.setattr(
        tool_backup_retention,
        "_revalidate_candidate",
        mutate_guard_during_candidate_preflight,
    )
    with pytest.raises(RuntimeError, match="restorable_guard_verification_failed"):
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )

    assert changed["done"] is True
    assert oldest.is_dir()
    assert older.is_dir()
    assert guard.is_dir()
    assert not list(Path(preview["backup_root"]).glob(".retention-*.tombstone"))


def test_backup_retention_keep_latest_uses_manifest_creation_time(backup_root):
    older = _complete_backup(
        backup_root,
        "zzzz_lexically_later_but_older",
        age=timedelta(days=90),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer = _complete_backup(
        backup_root,
        "aaaa_lexically_earlier_but_newer",
        age=timedelta(days=90),
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    shared_mtime = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
    os.utime(older, (shared_mtime, shared_mtime))
    os.utime(newer, (shared_mtime, shared_mtime))

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert {item["name"] for item in result["candidates"]} == {older.name}
    assert {item["name"] for item in result["protected"]} == {newer.name}


def test_backup_retention_minimum_age_protects_young_manifest_with_old_directory(
    backup_root,
):
    current = datetime.now(timezone.utc)
    protected = _complete_backup(
        backup_root,
        "young_manifest_old_directory",
        age=timedelta(days=90),
        created_at=current - timedelta(hours=2),
    )
    newest = _complete_backup(
        backup_root,
        "newest_manifest",
        age=timedelta(days=90),
        created_at=current - timedelta(hours=1),
    )

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert result["candidate_count"] == 0
    protected_by_name = {item["name"]: item for item in result["protected"]}
    assert protected_by_name[protected.name]["reason"] == "minimum_age"
    assert "keep_latest" in protected_by_name[newest.name]["reason"]


def test_backup_retention_minimum_age_expires_old_manifest_with_young_directory(
    backup_root,
):
    current = datetime.now(timezone.utc)
    candidate = _complete_backup(
        backup_root,
        "old_manifest_young_directory",
        age=timedelta(hours=2),
        created_at=current - timedelta(days=90),
    )
    newest = _complete_backup(
        backup_root,
        "newest_manifest",
        age=timedelta(hours=1),
        created_at=current - timedelta(hours=1),
    )

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert {item["name"] for item in result["candidates"]} == {candidate.name}
    assert {item["name"] for item in result["protected"]} == {newest.name}


@pytest.mark.parametrize(
    "manifest_mutation",
    [
        "duplicate_copied",
        "invalid_artifact_hash",
    ],
)
def test_backup_retention_ignores_malformed_complete_manifest(
    backup_root,
    manifest_mutation,
):
    candidate = _complete_backup(
        backup_root,
        "maintenance_malformed",
        age=timedelta(days=90),
    )
    manifest_path = candidate / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest_mutation == "duplicate_copied":
        manifest["copied"] = ["vector_lake.db", "vector_lake.db"]
    else:
        manifest["artifact_sha256"]["vector_lake.db"] = "A" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _set_age(candidate, age=timedelta(days=90))

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert result["candidate_count"] == 0
    assert candidate.is_dir()
    assert any(
        item["name"] == candidate.name and item["reason"] == "not_complete_backup"
        for item in result["ignored"]
    )


def test_backup_retention_ignores_exact_shape_empty_manifest_impostor(backup_root):
    impostor = backup_root / "maintenance_empty_impostor"
    impostor.mkdir()
    (impostor / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "label": impostor.name,
                "copied": [],
                "artifact_sha256": {},
                "database_runtime_generations": {},
                "database_runtime_generation_error": None,
                "projection_generation": None,
                "projection_canonical_generation": None,
                "canonical_projection_consistency": {
                    "status": "not_applicable",
                    "reason": "projection-pair-absent",
                },
                "restorable_as_consistent_canonical_projection_snapshot": False,
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    _set_age(impostor, age=timedelta(days=90))

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert result["candidate_count"] == 0
    assert impostor.is_dir()
    assert {item["name"] for item in result["ignored"]} == {impostor.name}


def test_backup_retention_apply_rejects_same_size_artifact_tamper(backup_root):
    _complete_backup(
        backup_root,
        "maintenance_new",
        age=timedelta(days=60),
        payload=b"newer",
    )
    candidate = _complete_backup(
        backup_root,
        "maintenance_old",
        age=timedelta(days=90),
        payload=b"before",
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))
    root_before = candidate.stat()
    artifact = candidate / "vector_lake.db"
    artifact_before = artifact.stat()
    with artifact.open("r+b") as handle:
        handle.seek(-len(b"after!"), os.SEEK_END)
        handle.write(b"after!")
    os.utime(artifact, ns=(artifact_before.st_atime_ns, artifact_before.st_mtime_ns))
    os.utime(candidate, ns=(root_before.st_atime_ns, root_before.st_mtime_ns))

    second = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))
    assert second["fingerprint"] == preview["fingerprint"]

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )
    )

    assert result["deleted_count"] == 0
    assert result["failed_count"] == 1
    assert "backup_artifact_hash_mismatch" in result["failed"][0]["reason"]
    assert candidate.is_dir()
    assert artifact.stat().st_size == artifact_before.st_size
    with artifact.open("rb") as handle:
        handle.seek(-len(b"after!"), os.SEEK_END)
        assert handle.read() == b"after!"
    assert not list(backup_root.glob(".retention-*.tombstone"))


def test_backup_retention_rechecks_candidate_after_quarantine_rename(
    backup_root,
    monkeypatch,
):
    _complete_backup(
        backup_root,
        "maintenance_new",
        age=timedelta(days=60),
        payload=b"newer",
    )
    candidate = _complete_backup(
        backup_root,
        "maintenance_old",
        age=timedelta(days=90),
        payload=b"candidate-before",
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))
    changed = {"done": False}
    original_guard_check = tool_backup_retention._revalidate_restorable_guard

    def mutate_candidate_after_first_verification(root, item):
        if not changed["done"]:
            artifact = candidate / "vector_lake.db"
            with artifact.open("r+b") as handle:
                handle.seek(-1, os.SEEK_END)
                last = handle.read(1)
                handle.seek(-1, os.SEEK_END)
                handle.write(bytes([last[0] ^ 0xFF]))
            changed["done"] = True
        return original_guard_check(root, item)

    monkeypatch.setattr(
        tool_backup_retention,
        "_revalidate_restorable_guard",
        mutate_candidate_after_first_verification,
    )
    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )
    )

    assert changed["done"] is True
    assert result["deleted_count"] == 0
    assert result["failed_count"] == 1
    assert "backup_artifact_hash_mismatch" in result["failed"][0]["reason"]
    assert not candidate.exists()
    tombstone = backup_root / result["failed"][0]["tombstone"]
    assert tombstone.is_dir()
    assert (tombstone / "vector_lake.db").is_file()


def test_backup_retention_ignores_symlink_candidate_and_preserves_target(
    backup_root,
):
    external = backup_root.parent / "external-backup"
    _complete_backup(
        external.parent,
        external.name,
        age=timedelta(days=90),
    )
    link = backup_root / "maintenance_link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    preview = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )
    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
            confirmation=preview["fingerprint"],
        )
    )

    assert result["deleted_count"] == 0
    assert link.is_symlink()
    assert external.is_dir()
    assert (external / "vector_lake.db").is_file()


def test_backup_retention_ignores_synthetic_reparse_in_candidate_tree(
    backup_root,
    monkeypatch,
):
    candidate = _complete_backup(
        backup_root,
        "maintenance_reparse_tree",
        age=timedelta(days=90),
    )
    marker = candidate / "vector_lake.db"
    marker_inode = marker.lstat().st_ino
    original = tool_backup_retention._is_reparse_stat

    def synthetic_reparse(details):
        return details.st_ino == marker_inode or original(details)

    monkeypatch.setattr(
        tool_backup_retention,
        "_is_reparse_stat",
        synthetic_reparse,
    )
    preview = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )
    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
            confirmation=preview["fingerprint"],
        )
    )

    assert preview["candidate_count"] == 0
    assert result["deleted_count"] == 0
    assert candidate.is_dir()
    assert marker.is_file()


def test_backup_retention_requires_exact_current_fingerprint(backup_root):
    candidate = _complete_backup(
        backup_root,
        "maintenance_old",
        age=timedelta(days=90),
    )
    preview = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )
    assert preview["candidate_count"] == 0

    expired_stage = _stage(
        backup_root,
        ".maintenance_20260402T000000000000Z.cccccccccccccccccccccccccccccccc.tmp",
        age=timedelta(hours=72),
    )
    with pytest.raises(ValueError, match="exactly match"):
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
            confirmation=preview["fingerprint"],
        )

    assert candidate.is_dir()
    assert expired_stage.is_dir()


def test_backup_retention_apply_deletes_matching_candidate_set(backup_root):
    protected = _complete_backup(
        backup_root,
        "maintenance_new",
        age=timedelta(days=60),
    )
    candidate = _complete_backup(
        backup_root,
        "maintenance_old",
        age=timedelta(days=90),
    )
    expired_stage = _stage(
        backup_root,
        ".maintenance_20260402T000000000000Z.cccccccccccccccccccccccccccccccc.tmp",
        age=timedelta(hours=72),
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )
    )

    assert result["applied"] is True
    assert result["failed_count"] == 0
    assert set(result["deleted"]) == {candidate.name, expired_stage.name}
    assert protected.is_dir()
    assert not candidate.exists()
    assert not expired_stage.exists()
    assert not list(backup_root.glob(".retention-*.tombstone"))


def test_backup_retention_leaves_identifiable_tombstone_on_delete_failure(
    backup_root,
    monkeypatch,
):
    _complete_backup(
        backup_root,
        "maintenance_new",
        age=timedelta(days=60),
    )
    candidate = _complete_backup(
        backup_root,
        "maintenance_old",
        age=timedelta(days=90),
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))

    def fail_delete(path):
        raise OSError("injected delete failure")

    monkeypatch.setattr(
        tool_backup_retention,
        "_remove_tree_no_follow",
        fail_delete,
    )
    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )
    )

    assert result["deleted_count"] == 0
    assert result["failed_count"] == 1
    failure = result["failed"][0]
    assert failure["name"] == candidate.name
    assert failure["tombstone"].endswith(".tombstone")
    assert not candidate.exists()
    tombstone = backup_root / failure["tombstone"]
    assert tombstone.is_dir()

    immediate = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))
    assert immediate["candidate_count"] == 0
    assert any(
        item["name"] == tombstone.name
        and item["reason"] == "retention_tombstone_not_expired"
        for item in immediate["ignored"]
    )


def test_backup_retention_recovers_expired_delete_tombstones(backup_root):
    complete_tombstone = _complete_backup(
        backup_root,
        ".retention-11111111111111111111111111111111.tombstone",
        age=timedelta(hours=72),
    )
    stage_tombstone = _stage(
        backup_root,
        ".retention-22222222222222222222222222222222.tombstone",
        age=timedelta(hours=72),
    )
    fresh_tombstone = _stage(
        backup_root,
        ".retention-33333333333333333333333333333333.tombstone",
        age=timedelta(hours=1),
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }

    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))

    assert {(item["name"], item["type"]) for item in preview["candidates"]} == {
        (complete_tombstone.name, "expired_retention_tombstone"),
        (stage_tombstone.name, "expired_retention_tombstone"),
    }
    assert any(
        item["name"] == fresh_tombstone.name
        and item["reason"] == "retention_tombstone_not_expired"
        for item in preview["ignored"]
    )

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )
    )

    assert result["failed_count"] == 0
    assert set(result["deleted"]) == {
        complete_tombstone.name,
        stage_tombstone.name,
    }
    assert not complete_tombstone.exists()
    assert not stage_tombstone.exists()
    assert fresh_tombstone.is_dir()


def test_backup_retention_preserves_only_verified_expired_tombstone(
    isolated_memory,
):
    db_store.init_db()
    indexer.generate_index()
    current = datetime.now(timezone.utc)
    backup = _create_verified_backup(
        "tombstone_only_guard",
        created_at=current - timedelta(days=90),
        age=timedelta(days=90),
    )
    tombstone = backup.parent / (
        ".retention-44444444444444444444444444444444.tombstone"
    )
    backup.rename(tombstone)
    _set_age(tombstone, age=timedelta(hours=72))

    preview = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert preview["candidate_count"] == 0
    assert preview["restorable_guard"]["name"] == tombstone.name
    protected = {item["name"]: item for item in preview["protected"]}
    assert "latest_restorable" in protected[tombstone.name]["reason"]
    assert protected[tombstone.name]["restorable"] is True


def test_backup_retention_can_delete_older_verified_tombstone_with_newer_guard(
    isolated_memory,
):
    db_store.init_db()
    indexer.generate_index()
    current = datetime.now(timezone.utc)
    old_backup = _create_verified_backup(
        "tombstone_old_guard",
        created_at=current - timedelta(days=90),
        age=timedelta(days=90),
    )
    old_tombstone = old_backup.parent / (
        ".retention-55555555555555555555555555555555.tombstone"
    )
    old_backup.rename(old_tombstone)
    _set_age(old_tombstone, age=timedelta(hours=72))
    newer = _create_verified_backup(
        "tombstone_newer_guard",
        created_at=current - timedelta(days=60),
        age=timedelta(days=60),
    )

    preview = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            keep_latest=1,
            min_age_days=30,
            stage_ttl_hours=24,
        )
    )

    assert preview["restorable_guard"]["name"] == newer.name
    assert {(item["name"], item["type"]) for item in preview["candidates"]} == {
        (old_tombstone.name, "expired_retention_tombstone")
    }
    protected = {item["name"]: item for item in preview["protected"]}
    assert "latest_restorable" in protected[newer.name]["reason"]


def test_backup_retention_fingerprint_covers_manifest_hash(backup_root):
    _complete_backup(
        backup_root,
        "maintenance_new",
        age=timedelta(days=60),
    )
    candidate = _complete_backup(
        backup_root,
        "maintenance_old",
        age=timedelta(days=90),
    )
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    first = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))
    before = candidate.stat()
    manifest_path = candidate / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["label"] = "maintenance_alt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    os.utime(candidate, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))

    assert first["candidates"][0]["mtime_ns"] == second["candidates"][0]["mtime_ns"]
    assert first["candidates"][0]["size"] == second["candidates"][0]["size"]
    assert (
        first["candidates"][0]["manifest_sha256"]
        != second["candidates"][0]["manifest_sha256"]
    )
    assert first["fingerprint"] != second["fingerprint"]


def test_backup_retention_empty_absent_root_apply_is_safe(tmp_path, monkeypatch):
    meta = tmp_path / ".meta"
    monkeypatch.setattr(tool_backup_retention, "peek_meta_dir", lambda: meta)
    kwargs = {
        "keep_latest": 1,
        "min_age_days": 30,
        "stage_ttl_hours": 24,
    }
    preview = json.loads(tool_backup_retention.backup_retention_maintenance(**kwargs))

    result = json.loads(
        tool_backup_retention.backup_retention_maintenance(
            dry_run=False,
            confirmation=preview["fingerprint"],
            **kwargs,
        )
    )

    assert result["root_state"] == "absent"
    assert result["applied"] is True
    assert result["deleted_count"] == 0
    assert not (meta / "backups").exists()
