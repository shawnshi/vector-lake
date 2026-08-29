import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from vector_lake import (
    backup_capacity,
    cli_app,
    db_store,
    governance_store,
    indexer,
    restore_snapshot,
)
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.tool_projection import create_maintenance_backup


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _page(title: str, body: str) -> str:
    return f"""---
id: {title.casefold().replace(' ', '_')}
title: {title}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-08-28T00:00:00+00:00
sources: []
strategic_scope: core
evidence_tier: primary
---
## 1. 编译事实

### 物理机制 (Mechanism)

{body}

## 2. 证据时间线

- [2026-08-28] [Observation] Recovery fixture.
"""


def _seed_snapshot(isolated_memory: Path) -> tuple[Path, Path, dict[str, str]]:
    (isolated_memory / "purpose.md").write_text(
        """---
purpose_version: "12.0"
intent_keywords: [test, restore]
scope:
  core: [test, restore]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
sir_registry:
  - id: SIR_RESTORE
    status: active
    review_after: 2099-01-01
    signal_keywords: [restore]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Restore test purpose.
""",
        encoding="utf-8",
    )
    page_key = "Concept_Restore-Snapshot"
    content = _page("Restore Snapshot", "Canonical snapshot body.")
    wiki_path = isolated_memory / "wiki" / f"{page_key}.md"
    execute_mutation_plan(wiki_path.name, content=content)
    indexer.generate_index()
    db_store.close_all_connections()
    backup_dir = Path(create_maintenance_backup("restore_snapshot"))
    db_store.close_all_connections()
    target_hashes = {
        name: _sha256(backup_dir / name)
        for name in (
            "vector_lake.db",
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        )
    }
    return backup_dir / "manifest.json", wiki_path, target_hashes


def _apply(receipt: Path) -> dict:
    preview = restore_snapshot.preview_restore_snapshot(receipt)
    assert preview["can_apply"] is True, preview
    return restore_snapshot.restore_snapshot_maintenance(
        maintenance_receipt=receipt,
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
    )


def _live_projection_hashes(isolated_memory: Path) -> dict[str, str]:
    wiki = isolated_memory / "wiki"
    return {
        name: _sha256(wiki / name)
        for name in (
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        )
    }


def _seed_large_legacy_v1_snapshot(
    isolated_memory: Path,
) -> tuple[Path, dict[str, str]]:
    _seed_snapshot(isolated_memory)
    from vector_lake.projection_format_v2 import (
        materialize_claim_graph,
        materialize_index,
    )
    from vector_lake.wiki_utils import (
        get_claim_graph_path,
        get_index_path,
        get_projection_manifest_path,
    )

    sidecar_path = get_projection_manifest_path()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    index_payload = materialize_index(get_index_path().parent, sidecar)
    graph_payload = materialize_claim_graph(get_index_path().parent, sidecar)
    index_payload["legacy_padding"] = ""
    target_bytes = 35_774_105
    encoded = json.dumps(
        index_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    index_payload["legacy_padding"] = "x" * (target_bytes - len(encoded))
    encoded = json.dumps(
        index_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) == target_bytes
    get_index_path().write_bytes(encoded)
    get_claim_graph_path().write_text(json.dumps(graph_payload), encoding="utf-8")
    sidecar_path.unlink()

    current_runtime = db_store.get_projection_runtime_v9()
    with db_store.transaction() as connection:
        db_store.mark_projection_runtime_rebuild_required(
            connection,
            expected_projection_generation=current_runtime["projection_generation"],
        )
    db_store.close_all_connections()

    estimated = backup_capacity.estimate_maintenance_backup_bytes()
    assert estimated >= int(target_bytes * 1.05)
    backup_dir = Path(create_maintenance_backup("restore_large_legacy_v1"))
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["projection_format"] == 1
    assert manifest["artifact_bytes"]["index.json"] == target_bytes
    target_hashes = {
        name: _sha256(backup_dir / name)
        for name in (
            "vector_lake.db",
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        )
    }
    return backup_dir / "manifest.json", target_hashes


def test_restore_large_legacy_v1_backup_preserves_exact_hashes(isolated_memory):
    receipt, target = _seed_large_legacy_v1_snapshot(isolated_memory)
    live_index = isolated_memory / "wiki" / "index.json"
    live_index.write_text("{}", encoding="utf-8")
    db_store.close_all_connections()

    preview = restore_snapshot.preview_restore_snapshot(receipt)
    assert preview["can_apply"] is True, preview
    assert preview["maintenance_receipt"]["projection_format"] == 1
    result = _apply(receipt)

    assert result["applied"] is True
    assert _live_projection_hashes(isolated_memory) == {
        name: target[name]
        for name in (
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        )
    }


def test_restore_rejects_arbitrary_database_and_tampered_backup(isolated_memory):
    receipt, _wiki_path, _target = _seed_snapshot(isolated_memory)
    arbitrary = receipt.parent / "vector_lake.db"

    invalid = restore_snapshot.preview_restore_snapshot(arbitrary)
    assert invalid["can_apply"] is False
    assert "maintenance_receipt_must_name_manifest_json" in invalid["issues"]

    preview = restore_snapshot.preview_restore_snapshot(receipt)
    (receipt.parent / "index.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot apply|changed|mismatch"):
        restore_snapshot.restore_snapshot_maintenance(
            maintenance_receipt=receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
        )


def test_restore_round_trip_from_damaged_database_and_projection(isolated_memory):
    receipt, _wiki_path, target = _seed_snapshot(isolated_memory)
    database = db_store.peek_db_path().resolve()
    database.write_bytes(b"damaged-current-database")
    (isolated_memory / "wiki" / "index.json").write_text("{}", encoding="utf-8")

    preview = restore_snapshot.preview_restore_snapshot(receipt)
    assert preview["can_apply"] is True
    assert preview["current"]["database"]["quick_check"] == "damaged"
    assert preview["projection_action"] == "restore_committed_pair"
    result = _apply(receipt)

    assert result["applied"] is True
    assert _sha256(database) == target["vector_lake.db"]
    assert _live_projection_hashes(isolated_memory) == {
        name: target[name]
        for name in (
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        )
    }
    with sqlite3.connect(
        f"{database.as_uri()}?mode=ro&immutable=1", uri=True
    ) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


def test_restore_repairs_one_damaged_projection_without_replacing_database(
    isolated_memory,
):
    receipt, _wiki_path, target = _seed_snapshot(isolated_memory)
    database = db_store.peek_db_path().resolve()
    shutil.copyfile(receipt.parent / "vector_lake.db", database)
    database_before = _sha256(database)
    (isolated_memory / "wiki" / "claim_graph.json").write_text(
        "{}", encoding="utf-8"
    )

    preview = restore_snapshot.preview_restore_snapshot(receipt)
    assert preview["database_action"] == "preserve_verified_target_database"
    assert preview["projection_action"] == "restore_committed_pair"
    result = _apply(receipt)

    assert result["applied"] is True
    assert _sha256(database) == database_before == target["vector_lake.db"]
    assert _live_projection_hashes(isolated_memory) == {
        name: target[name]
        for name in (
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        )
    }


def test_restore_fingerprint_binds_wiki_identity_and_active_writer(
    isolated_memory,
):
    receipt, _wiki_path, _target = _seed_snapshot(isolated_memory)
    first = restore_snapshot.preview_restore_snapshot(receipt)
    assert first["no_writers"]["observed_no_writer"] is True
    assert first["wiki_action"]["identity"]

    unrelated = isolated_memory / "wiki" / "operator-note.md"
    unrelated.write_text("changed after preview", encoding="utf-8")
    second = restore_snapshot.preview_restore_snapshot(receipt)
    assert second["fingerprint"] != first["fingerprint"]
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        restore_snapshot.restore_snapshot_maintenance(
            maintenance_receipt=receipt,
            apply=True,
            confirmation=first["fingerprint"],
            confirm_no_writers=True,
        )

    database = db_store.peek_db_path().resolve()
    with sqlite3.connect(database, timeout=0, isolation_level=None) as writer:
        writer.execute("BEGIN IMMEDIATE")
        blocked = restore_snapshot.preview_restore_snapshot(receipt)
        writer.execute("ROLLBACK")
    assert blocked["can_apply"] is False
    assert "active_sqlite_writer_detected" in blocked["issues"]


def test_restore_rebuilds_only_missing_canonical_wiki(isolated_memory):
    receipt, wiki_path, _target = _seed_snapshot(isolated_memory)
    unrelated = isolated_memory / "wiki" / "operator-notes.md"
    unrelated.write_bytes(b"operator-owned\r\n")
    wiki_path.unlink()

    preview = restore_snapshot.preview_restore_snapshot(receipt)
    assert preview["wiki_action"]["missing"] == ["Concept_Restore-Snapshot.md"], (
        preview["issues"],
        preview["wiki_action"],
    )
    result = _apply(receipt)

    assert result["post"]["wiki"]["missing"] == []
    assert wiki_path.is_file()
    assert unrelated.read_bytes() == b"operator-owned\r\n"


def test_restore_rebuilds_drifted_canonical_wiki_and_backs_up_prior_page(
    isolated_memory,
):
    receipt, wiki_path, _target = _seed_snapshot(isolated_memory)
    unrelated = isolated_memory / "wiki" / "operator-notes.md"
    unrelated.write_bytes(b"operator-owned\r\n")
    drifted = _page("Restore Snapshot", "Drifted current body.")
    wiki_path.write_text(drifted, encoding="utf-8", newline="\n")
    drifted_hash = _sha256(wiki_path)

    preview = restore_snapshot.preview_restore_snapshot(receipt)
    assert preview["can_apply"] is True, preview
    assert [item["filename"] for item in preview["wiki_action"]["rebuild"]] == [
        "Concept_Restore-Snapshot.md"
    ]
    result = _apply(receipt)

    assert result["post"]["wiki"]["missing"] == []
    assert result["post"]["wiki"]["rebuilt"][0]["filename"] == wiki_path.name
    assert governance_store.canonical_page_version_from_content(
        wiki_path.name, wiki_path.read_text(encoding="utf-8")
    ) == preview["wiki_action"]["rebuild"][0]["canonical_version"]
    assert unrelated.read_bytes() == b"operator-owned\r\n"

    forward_manifest = Path(
        result["forward_recovery"]["manifest_path"]
    )
    manifest = restore_snapshot._read_json_stable(forward_manifest)[0]
    backed_up = [
        item
        for item in manifest["artifacts"]
        if item.get("source_path") == str(wiki_path.resolve())
    ]
    assert len(backed_up) == 1
    assert backed_up[0]["sha256"] == drifted_hash


@pytest.mark.parametrize(
    ("checkpoint", "expected_recovery_action"),
    [
        ("after_database_replace", "resume_projection_restore"),
        ("after_projection_publish", "publish_completed_receipt"),
        ("after_wiki_publish", "publish_completed_receipt"),
    ],
)
def test_restore_keeps_forward_bundle_and_resumes_after_interruption(
    isolated_memory,
    monkeypatch,
    checkpoint,
    expected_recovery_action,
):
    receipt, wiki_path, _target = _seed_snapshot(isolated_memory)
    governance_store.upsert_entity(
        "post_snapshot",
        {
            "entity_id": "post_snapshot",
            "canonical_name": "Post Snapshot",
            "page_key": "Concept_Post-Snapshot",
            "type": "concept",
            "raw_text": "Post-snapshot state to preserve in the forward bundle.",
        },
    )
    indexer.generate_index()
    db_store.close_all_connections()
    if checkpoint == "after_wiki_publish":
        wiki_path.write_text(
            _page("Restore Snapshot", "Drift before injected Wiki failure."),
            encoding="utf-8",
            newline="\n",
        )
    preview = restore_snapshot.preview_restore_snapshot(receipt)

    def fail_at(observed: str) -> None:
        if observed == checkpoint:
            raise RuntimeError(f"injected:{checkpoint}")

    monkeypatch.setattr(restore_snapshot, "_TEST_FAULT_HOOK", fail_at)
    with pytest.raises(RuntimeError, match=f"injected:{checkpoint}"):
        restore_snapshot.restore_snapshot_maintenance(
            maintenance_receipt=receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
        )

    pending = list(
        (isolated_memory / "wiki" / ".meta" / "restore-snapshot-receipts").glob(
            "*.pending.json"
        )
    )
    bundles = list(
        (isolated_memory / "wiki" / ".meta" / "restore-snapshot-forward").glob(
            "*"
        )
    )
    assert len(pending) == 1
    assert len(bundles) == 1

    monkeypatch.setattr(restore_snapshot, "_TEST_FAULT_HOOK", None)
    recovery = restore_snapshot.preview_restore_snapshot(receipt)
    assert recovery["recovery_action"] == expected_recovery_action
    resumed = restore_snapshot.restore_snapshot_maintenance(
        maintenance_receipt=receipt,
        apply=True,
        confirmation=recovery["fingerprint"],
        confirm_no_writers=True,
    )
    assert resumed["recovery_action"] == "resumed_and_completed_restore"


@pytest.mark.parametrize(
    ("checkpoint", "expected_recovery_action"),
    [
        ("after_database_replace", "resume_projection_restore"),
        ("after_projection_publish", "publish_completed_receipt"),
        ("after_wiki_publish", "publish_completed_receipt"),
    ],
)
def test_restore_resumes_after_hard_process_exit(
    isolated_memory,
    checkpoint,
    expected_recovery_action,
):
    receipt, wiki_path, _target = _seed_snapshot(isolated_memory)
    governance_store.upsert_entity(
        "hard_exit_state",
        {
            "entity_id": "hard_exit_state",
            "canonical_name": "Hard Exit State",
            "page_key": "Concept_Hard-Exit-State",
            "type": "concept",
            "raw_text": "State preserved only by the forward recovery bundle.",
        },
    )
    indexer.generate_index()
    db_store.close_all_connections()
    if checkpoint == "after_wiki_publish":
        wiki_path.write_text(
            _page("Restore Snapshot", "Drift before hard Wiki exit."),
            encoding="utf-8",
            newline="\n",
        )
    child = """
import os
import sys
from pathlib import Path
from vector_lake import restore_snapshot

receipt = Path(sys.argv[1])
checkpoint = sys.argv[2]
preview = restore_snapshot.preview_restore_snapshot(receipt)
def terminate(observed):
    if observed == checkpoint:
        os._exit(91)
restore_snapshot._TEST_FAULT_HOOK = terminate
restore_snapshot.restore_snapshot_maintenance(
    maintenance_receipt=receipt,
    apply=True,
    confirmation=preview["fingerprint"],
    confirm_no_writers=True,
)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", child, str(receipt), checkpoint],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 91, (result.stdout, result.stderr)

    recovery = restore_snapshot.preview_restore_snapshot(receipt)
    assert recovery["recovery_action"] == expected_recovery_action
    resumed = restore_snapshot.restore_snapshot_maintenance(
        maintenance_receipt=receipt,
        apply=True,
        confirmation=recovery["fingerprint"],
        confirm_no_writers=True,
    )
    assert resumed["recovery_action"] == "resumed_and_completed_restore"


def test_restore_forward_bundle_tamper_fails_closed(isolated_memory, monkeypatch):
    receipt, _wiki_path, _target = _seed_snapshot(isolated_memory)
    governance_store.upsert_entity(
        "forward_state",
        {
            "entity_id": "forward_state",
            "canonical_name": "Forward State",
            "page_key": "Concept_Forward-State",
            "type": "concept",
        },
    )
    indexer.generate_index()
    db_store.close_all_connections()
    preview = restore_snapshot.preview_restore_snapshot(receipt)

    def fail_after_database(observed: str) -> None:
        if observed == "after_database_replace":
            raise RuntimeError("injected:after_database_replace")

    monkeypatch.setattr(restore_snapshot, "_TEST_FAULT_HOOK", fail_after_database)
    with pytest.raises(RuntimeError, match="injected"):
        restore_snapshot.restore_snapshot_maintenance(
            maintenance_receipt=receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
        )
    forward_db = next(
        (isolated_memory / "wiki" / ".meta" / "restore-snapshot-forward").glob(
            "*/vector_lake.db"
        )
    )
    forward_db.write_bytes(b"tampered-forward-bundle")
    monkeypatch.setattr(restore_snapshot, "_TEST_FAULT_HOOK", None)

    recovery = restore_snapshot.preview_restore_snapshot(receipt)
    assert recovery["can_apply"] is False
    assert "forward_recovery_bundle_invalid" in recovery["issues"]


def test_completed_restore_receipt_is_idempotently_queryable(isolated_memory):
    receipt, _wiki_path, _target = _seed_snapshot(isolated_memory)
    result = _apply(receipt)
    completed = Path(result["receipt_path"])
    assert completed.is_file()

    query = restore_snapshot.preview_restore_snapshot(receipt)
    assert query["recovery_action"] == "already_completed"
    replay = restore_snapshot.restore_snapshot_maintenance(
        maintenance_receipt=receipt,
        apply=True,
        confirmation=query["fingerprint"],
        confirm_no_writers=True,
    )
    assert replay["no_op"] is True
    assert replay["receipt_path"] == str(completed)


def test_restore_snapshot_cli_is_preview_first_and_apply_is_heavy(
    monkeypatch,
    capsys,
):
    receipt = str(Path.cwd() / "manifest.json")
    preview = cli_app.build_parser().parse_args(
        ["restore-snapshot", "--maintenance-receipt", receipt]
    )
    apply = cli_app.build_parser().parse_args(
        [
            "restore-snapshot",
            "--maintenance-receipt",
            receipt,
            "--apply",
            "--confirm-fingerprint",
            "sha256:abc",
            "--confirm-no-writers",
        ]
    )

    assert preview.apply is False
    assert cli_app._cli_heavy_task_policy(preview) is None
    assert cli_app._cli_heavy_task_policy(apply) == ("maintenance", 1800.0)

    calls = []

    def fake_restore(**kwargs):
        calls.append(kwargs)
        return {"contract": "vector-lake-snapshot-restore-plan/v1", "dry_run": True}

    monkeypatch.setattr(cli_app, "bootstrap_runtime_paths", lambda **_kwargs: {})
    monkeypatch.setattr(cli_app, "_load_env", lambda: None)
    monkeypatch.setattr(
        restore_snapshot, "restore_snapshot_maintenance", fake_restore
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "restore-snapshot", "--maintenance-receipt", receipt],
    )

    assert cli_app.main() == 0
    assert calls == [
        {
            "maintenance_receipt": receipt,
            "apply": False,
            "confirmation": "",
            "confirm_no_writers": False,
        }
    ]
    capsys.readouterr()
