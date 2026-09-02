import hashlib
import json
from pathlib import Path

import pytest

from vector_lake import (
    db_store,
    governance_store,
    mcp_server,
    mutation_coordinator,
    tool_wiki_batch,
)
from tests.test_mutation_coordinator import _write_purpose_contract


def _manifest(tmp_path: Path, files: dict[str, bytes], *, maintenance=()):
    operations = []
    payloads = {}
    versions = {}
    paths = {}
    for index, (filename, current_bytes) in enumerate(files.items(), start=1):
        current_path = tmp_path / filename
        current_path.write_bytes(current_bytes)
        payload_file = f"C:/approved/scratch/{filename}"
        payloads[payload_file] = current_bytes.decode("utf-8") + "\nupdated"
        versions[filename.removesuffix(".md")] = f"version-{index}"
        paths[filename] = current_path
        operations.append(
            {
                "filename": filename,
                "payload_file": payload_file,
                "expected_version": f"version-{index}",
                "expected_projection_hash": hashlib.sha256(current_bytes).hexdigest(),
            }
        )
    manifest = {
        "schema_version": 1,
        "operations": operations,
        "schema_maintenance_filenames": list(maintenance),
    }
    return json.dumps(manifest), payloads, versions, paths


def _patch_plan_dependencies(monkeypatch, versions, paths):
    prepared = []

    def prepare(mutations, **kwargs):
        prepared.append((mutations, kwargs))
        return mutations

    monkeypatch.setattr(tool_wiki_batch, "_prepare_mutations", prepare)
    monkeypatch.setattr(
        tool_wiki_batch.mutation_coordinator,
        "validate_mutation_batch_metadata",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(tool_wiki_batch.db_store, "init_db", lambda: None)
    monkeypatch.setattr(
        tool_wiki_batch.governance_store,
        "canonical_page_versions",
        lambda _keys: versions,
    )
    monkeypatch.setattr(
        tool_wiki_batch,
        "resolve_wiki_mutation_path",
        lambda filename, **_kwargs: paths[filename],
    )
    return prepared


def test_wiki_batch_preview_binds_payload_versions_and_projection_hashes(
    tmp_path,
    monkeypatch,
):
    manifest, payloads, versions, paths = _manifest(
        tmp_path,
        {"Concept_A.md": b"old-a", "Concept_B.md": b"old-b"},
        maintenance=["Concept_B.md"],
    )
    prepared = _patch_plan_dependencies(monkeypatch, versions, paths)

    result = tool_wiki_batch.run_wiki_batch(
        manifest,
        payloads.__getitem__,
        dry_run=True,
        allowed_schema_maintenance_filenames=frozenset({"Concept_B.md"}),
    )

    assert result["ok"] is True
    assert result["committed"] is False
    assert result["operation_count"] == 2
    assert result["schema_maintenance_count"] == 1
    assert result["fingerprint"].startswith("sha256:")
    assert [item["validation_mode"] for item in result["operations"]] == [
        "full",
        "schema",
    ]
    assert prepared[0][1] == {
        "validation_mode": "full",
        "schema_maintenance_filenames": ["Concept_B.md"],
    }


def test_wiki_batch_apply_uses_one_atomic_mutation_call(tmp_path, monkeypatch):
    manifest, payloads, versions, paths = _manifest(
        tmp_path,
        {"Concept_A.md": b"old-a", "Concept_B.md": b"old-b"},
        maintenance=["Concept_B.md"],
    )
    _patch_plan_dependencies(monkeypatch, versions, paths)
    calls = []

    def execute(mutations, **kwargs):
        calls.append((mutations, kwargs))
        return {
            "ok": True,
            "committed": True,
            "outbox_ids": [41, 42],
            "deferred": [],
            "post_commit_warnings": ["private detail"],
        }

    monkeypatch.setattr(tool_wiki_batch, "execute_mutation_batch", execute)
    allowed = frozenset({"Concept_B.md"})
    preview = tool_wiki_batch.run_wiki_batch(
        manifest,
        payloads.__getitem__,
        dry_run=True,
        allowed_schema_maintenance_filenames=allowed,
    )
    result = tool_wiki_batch.run_wiki_batch(
        manifest,
        payloads.__getitem__,
        dry_run=False,
        confirmation=preview["fingerprint"],
        allowed_schema_maintenance_filenames=allowed,
    )

    assert result["ok"] is True
    assert result["committed"] is True
    assert result["outbox_ids"] == [41, 42]
    assert result["post_commit_warnings"] == ["post_commit_follow_up_warning"]
    assert len(calls) == 1
    mutations, kwargs = calls[0]
    assert [mutation["filename"] for mutation in mutations] == [
        "Concept_A.md",
        "Concept_B.md",
    ]
    assert kwargs == {
        "validation_mode": "full",
        "origin": "mcp_write_wiki_batch",
        "return_details": True,
        "schema_maintenance_filenames": ["Concept_B.md"],
    }


def test_wiki_batch_requires_current_exact_confirmation(tmp_path, monkeypatch):
    manifest, payloads, versions, paths = _manifest(
        tmp_path,
        {"Concept_A.md": b"old-a"},
    )
    _patch_plan_dependencies(monkeypatch, versions, paths)

    with pytest.raises(ValueError, match="exactly match"):
        tool_wiki_batch.run_wiki_batch(
            manifest,
            payloads.__getitem__,
            dry_run=False,
            confirmation="sha256:" + "0" * 64,
        )


def test_wiki_batch_rejects_system_page_before_loading_page_payload(tmp_path):
    manifest, _payloads, _versions, _paths = _manifest(
        tmp_path,
        {"System_Protected.md": b"old"},
    )
    loaded = []

    def payload_loader(path):
        loaded.append(path)
        raise AssertionError("payload must not be loaded")

    with pytest.raises(
        tool_wiki_batch.SystemPageWriteNotAuthorized,
        match="disabled",
    ):
        tool_wiki_batch.build_wiki_batch_plan(manifest, payload_loader)
    assert loaded == []


def test_wiki_batch_validates_all_metadata_before_loading_any_payload(tmp_path):
    manifest, payloads, _versions, _paths = _manifest(
        tmp_path,
        {"Concept_Ordinary.md": b"old", "System_Protected.md": b"old"},
    )
    loaded = []

    with pytest.raises(tool_wiki_batch.SystemPageWriteNotAuthorized):
        tool_wiki_batch.build_wiki_batch_plan(
            manifest,
            lambda path: loaded.append(path) or payloads[path],
        )
    assert loaded == []


def test_wiki_batch_rejects_late_traversal_before_loading_any_payload(tmp_path):
    manifest, payloads, _versions, _paths = _manifest(
        tmp_path,
        {"Concept_Ordinary.md": b"old", "Concept_Invalid.md": b"old"},
    )
    parsed = json.loads(manifest)
    parsed["operations"][1]["filename"] = "../Concept_Invalid.md"
    loaded = []

    with pytest.raises(ValueError, match="path separators"):
        tool_wiki_batch.build_wiki_batch_plan(
            json.dumps(parsed),
            lambda path: loaded.append(path) or payloads[path],
        )
    assert loaded == []


def test_wiki_batch_rejects_untrusted_schema_maintenance_before_payload_read(
    tmp_path,
):
    manifest, payloads, _versions, _paths = _manifest(
        tmp_path,
        {"Concept_Legacy.md": b"old"},
        maintenance=["Concept_Legacy.md"],
    )
    loaded = []

    with pytest.raises(tool_wiki_batch.SchemaMaintenanceNotAuthorized):
        tool_wiki_batch.build_wiki_batch_plan(
            manifest,
            lambda path: loaded.append(path) or payloads[path],
        )
    assert loaded == []


def test_wiki_batch_rejects_more_than_fifty_operations_before_loading_payloads():
    operation = {
        "filename": "Concept_A.md",
        "payload_file": "C:/approved/scratch/Concept_A.md",
        "expected_version": "version-a",
        "expected_projection_hash": "0" * 64,
    }
    manifest = json.dumps(
        {
            "schema_version": 1,
            "operations": [
                {**operation, "filename": f"Concept_{index}.md"}
                for index in range(51)
            ],
            "schema_maintenance_filenames": [],
        }
    )

    with pytest.raises(ValueError, match="exceeds 50"):
        tool_wiki_batch.build_wiki_batch_plan(
            manifest,
            lambda _path: (_ for _ in ()).throw(AssertionError("unexpected read")),
        )


def test_wiki_batch_enforces_configured_and_hard_aggregate_byte_bound(
    tmp_path,
    monkeypatch,
):
    manifest, payloads, versions, paths = _manifest(
        tmp_path,
        {"Concept_A.md": b"old"},
    )
    payloads["C:/approved/scratch/Concept_A.md"] = "seven77"
    _patch_plan_dependencies(monkeypatch, versions, paths)
    monkeypatch.setenv("VECTOR_LAKE_WIKI_BATCH_MAX_BYTES", "100")
    monkeypatch.setattr(tool_wiki_batch, "_HARD_MAX_BATCH_BYTES", 6)

    with pytest.raises(ValueError, match="payload bytes exceed 6"):
        tool_wiki_batch.build_wiki_batch_plan(
            manifest,
            payloads.__getitem__,
        )


def test_wiki_batch_rejects_projection_and_canonical_drift_after_preview(
    tmp_path,
    monkeypatch,
):
    manifest, payloads, versions, paths = _manifest(
        tmp_path,
        {"Concept_A.md": b"old-a"},
    )
    _patch_plan_dependencies(monkeypatch, versions, paths)
    preview = tool_wiki_batch.run_wiki_batch(
        manifest,
        payloads.__getitem__,
        dry_run=True,
    )

    paths["Concept_A.md"].write_bytes(b"changed")
    with pytest.raises(ValueError, match="Projection changed"):
        tool_wiki_batch.run_wiki_batch(
            manifest,
            payloads.__getitem__,
            dry_run=False,
            confirmation=preview["fingerprint"],
        )

    paths["Concept_A.md"].write_bytes(b"old-a")
    versions["Concept_A"] = "concurrent-version"
    with pytest.raises(ValueError, match="Canonical version changed"):
        tool_wiki_batch.run_wiki_batch(
            manifest,
            payloads.__getitem__,
            dry_run=False,
            confirmation=preview["fingerprint"],
        )


def _valid_source(page_id: str, title: str, body: str) -> str:
    return f"""---
id: {page_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-09-02T00:00:00+00:00
sources: [raw/{page_id}.md]
strategic_scope: core
evidence_tier: primary
---
{body}
"""


def test_wiki_batch_real_state_drift_rejects_without_new_outbox(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    filename = "Source_Batch-Drift.md"
    page_key = filename.removesuffix(".md")
    original = _valid_source("source_batch_drift", "Batch Drift", "Original.")
    mutation_coordinator.execute_mutation_batch(
        [{"filename": filename, "content": original, "is_delete": False}]
    )
    wiki_path = isolated_memory / "wiki" / filename
    version = governance_store.canonical_page_versions({page_key})[page_key]
    payload_path = "payload://Source_Batch-Drift.md"
    payloads = {payload_path: original.replace("Original.", "Updated.")}
    manifest = json.dumps(
        {
            "schema_version": 1,
            "operations": [
                {
                    "filename": filename,
                    "payload_file": payload_path,
                    "expected_version": version,
                    "expected_projection_hash": hashlib.sha256(
                        wiki_path.read_bytes()
                    ).hexdigest(),
                }
            ],
            "schema_maintenance_filenames": [],
        }
    )
    preview = tool_wiki_batch.run_wiki_batch(
        manifest,
        payloads.__getitem__,
        dry_run=True,
    )
    outbox_before = db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0]

    wiki_path.write_text(original + "\nconcurrent projection", encoding="utf-8")
    with pytest.raises(ValueError, match="Projection changed"):
        tool_wiki_batch.run_wiki_batch(
            manifest,
            payloads.__getitem__,
            dry_run=False,
            confirmation=preview["fingerprint"],
        )
    assert governance_store.canonical_page_versions({page_key})[page_key] == version
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0] == outbox_before

    wiki_path.write_text(original, encoding="utf-8")
    row = db_store.get_connection().execute(
        "SELECT entity_id, data_json FROM entities "
        "WHERE json_extract(data_json, '$.page_key') = ? LIMIT 1",
        (page_key,),
    ).fetchone()
    record = json.loads(row["data_json"])
    record["raw_text"] = "Concurrent canonical state."
    governance_store.upsert_entity(row["entity_id"], record)
    concurrent_version = governance_store.canonical_page_versions({page_key})[page_key]
    assert concurrent_version != version
    with pytest.raises(ValueError, match="Canonical version changed"):
        tool_wiki_batch.run_wiki_batch(
            manifest,
            payloads.__getitem__,
            dry_run=False,
            confirmation=preview["fingerprint"],
        )
    assert (
        governance_store.canonical_page_versions({page_key})[page_key]
        == concurrent_version
    )
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0] == outbox_before


def test_wiki_batch_rolls_back_every_canonical_page_on_second_enqueue_failure(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    originals = {
        "Source_Batch-A.md": _valid_source("source_batch_a", "Batch A", "Original A."),
        "Source_Batch-B.md": _valid_source("source_batch_b", "Batch B", "Original B."),
    }
    mutation_coordinator.execute_mutation_batch(
        [
            {"filename": filename, "content": content, "is_delete": False}
            for filename, content in originals.items()
        ]
    )
    wiki_dir = isolated_memory / "wiki"
    versions = governance_store.canonical_page_versions(
        {filename.removesuffix(".md") for filename in originals}
    )
    payloads = {
        f"payload://{filename}": content.replace("Original", "Updated")
        for filename, content in originals.items()
    }
    manifest = json.dumps(
        {
            "schema_version": 1,
            "operations": [
                {
                    "filename": filename,
                    "payload_file": f"payload://{filename}",
                    "expected_version": versions[filename.removesuffix(".md")],
                    "expected_projection_hash": hashlib.sha256(
                        (wiki_dir / filename).read_bytes()
                    ).hexdigest(),
                }
                for filename in originals
            ],
            "schema_maintenance_filenames": [],
        }
    )
    preview = tool_wiki_batch.run_wiki_batch(
        manifest,
        payloads.__getitem__,
        dry_run=True,
    )
    versions_before = governance_store.canonical_page_versions(set(versions))
    outbox_before = db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0]
    real_enqueue = db_store.enqueue_mutation
    calls = 0

    def fail_second_enqueue(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second enqueue failure")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(db_store, "enqueue_mutation", fail_second_enqueue)
    with pytest.raises(RuntimeError, match="second enqueue failure"):
        tool_wiki_batch.run_wiki_batch(
            manifest,
            payloads.__getitem__,
            dry_run=False,
            confirmation=preview["fingerprint"],
        )

    assert governance_store.canonical_page_versions(set(versions)) == versions_before
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0] == outbox_before
    for filename, content in originals.items():
        assert (wiki_dir / filename).read_text(encoding="utf-8") == content


def test_public_wiki_batch_uses_exact_host_schema_maintenance_allowlist(
    tmp_path,
    monkeypatch,
):
    manifest, payloads, versions, paths = _manifest(
        tmp_path,
        {"Concept_Legacy.md": b"old"},
        maintenance=["Concept_Legacy.md"],
    )
    _patch_plan_dependencies(monkeypatch, versions, paths)
    manifest_path = "C:/approved/scratch/manifest.json"
    page_reads = []

    def load(path):
        if path == manifest_path:
            return manifest
        page_reads.append(path)
        return payloads[path]

    monkeypatch.setattr(mcp_server, "_read_payload", load)
    monkeypatch.setenv(
        "VECTOR_LAKE_WIKI_BATCH_SCHEMA_MAINTENANCE_ALLOWLIST",
        json.dumps(["Concept_Legacy.md"]),
    )
    accepted = json.loads(mcp_server.write_wiki_batch(manifest_path))
    assert accepted["ok"] is True
    assert page_reads == ["C:/approved/scratch/Concept_Legacy.md"]

    page_reads.clear()
    monkeypatch.setenv(
        "VECTOR_LAKE_WIKI_BATCH_SCHEMA_MAINTENANCE_ALLOWLIST",
        json.dumps(["Concept_Legacy-Near.md"]),
    )
    rejected = json.loads(mcp_server.write_wiki_batch(manifest_path))
    assert rejected["ok"] is False
    assert rejected["error_code"] == "schema_maintenance_forbidden"
    assert page_reads == []


def test_public_wiki_batch_sanitizes_malformed_host_allowlist(monkeypatch):
    leaked = "C:/private/operator/secret"
    monkeypatch.setattr(mcp_server, "_read_payload", lambda _path: "{}")
    monkeypatch.setenv(
        "VECTOR_LAKE_WIKI_BATCH_SCHEMA_MAINTENANCE_ALLOWLIST",
        leaked,
    )

    raw_receipt = mcp_server.write_wiki_batch("C:/approved/scratch/manifest.json")
    receipt = json.loads(raw_receipt)
    assert receipt["error_code"] == "invalid_request"
    assert leaked not in raw_receipt


def test_public_wiki_batch_failure_receipt_is_sanitized(monkeypatch):
    leaked = "C:/private/operator/secret.json traceback sentinel"
    monkeypatch.setattr(
        mcp_server,
        "_read_payload",
        lambda _path: (_ for _ in ()).throw(ValueError(leaked)),
    )

    raw_receipt = mcp_server.write_wiki_batch(
        "C:/private/operator/manifest.json",
        dry_run=True,
    )
    receipt = json.loads(raw_receipt)

    assert receipt["ok"] is False
    assert receipt["committed"] is False
    assert receipt["error_code"] == "invalid_request"
    assert "C:/private" not in raw_receipt
    assert "traceback sentinel" not in raw_receipt
