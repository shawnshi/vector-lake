"""Contract tests for the fingerprinted Wiki page deletion batch."""

from __future__ import annotations

import hashlib
import json

import pytest

from vector_lake import db_store, governance_store
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.tool_wiki_delete import (
    SystemPageDeleteNotAuthorized,
    delete_wiki_batch,
)
from vector_lake.wiki_utils import get_wiki_dir


def _source_page_markdown(title: str, marker: str) -> str:
    """Minimal schema-valid Source page mirroring ingest-generated seeds."""
    return (
        "---\n"
        f'id: "20260912_abc123"\n'
        f'title: "{title}"\n'
        "aliases: []\n"
        'type: "source"\n'
        'domain: "Medical_IT"\n'
        'topic_cluster: "General"\n'
        'status: "Active"\n'
        'epistemic-status: "seed"\n'
        "ttl: 365\n"
        'memory_type: "fact"\n'
        "categories: [\"Uncategorized\"]\n"
        "tags: []\n"
        'created: "2026-09-12"\n'
        'updated: "2026-09-12"\n'
        "sources: []\n"
        'strategic_scope: "core"\n'
        'evidence_tier: "code-availability"\n'
        "---\n"
        f"# {title}\n\n"
        f"{marker}\n"
    )


def _write_page(filename: str, content: str) -> str:
    path = get_wiki_dir() / filename
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path, operations, archive=True) -> str:
    payload = {
        "schema_version": 1,
        "operations": operations,
        "archive": archive,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _operations_for(filename: str, projection_hash: str, version: str = "") -> dict:
    return {
        "filename": filename,
        "expected_version": version,
        "expected_projection_hash": projection_hash,
    }


@pytest.fixture(autouse=True)
def _no_live_state(isolate_test_runtime):
    db_store.init_db()
    yield


def test_preview_requires_matching_projection_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    _write_page("Source_briefing-alpha.md", _source_page_markdown("Alpha", "body"))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_operations_for("Source_briefing-alpha.md", "0" * 64)],
    )
    with pytest.raises(ValueError, match="Projection changed"):
        delete_wiki_batch(manifest, dry_run=True)


def test_preview_requires_canonical_version_match(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    projection_hash = _write_page(
        "Source_briefing-alpha.md", _source_page_markdown("Alpha", "body")
    )
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_operations_for("Source_briefing-alpha.md", projection_hash, "f" * 64)],
    )
    with pytest.raises(ValueError, match="Canonical version changed"):
        delete_wiki_batch(manifest, dry_run=True)


def test_refuses_page_that_other_pages_link_to(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    projection_hash = _write_page(
        "Source_briefing-alpha.md", _source_page_markdown("Alpha", "body")
    )
    _write_page(
        "Concept_Referrer.md",
        "---\nid: c1\ntitle: Referrer\ntype: concept\ndomain: Medical_IT\n"
        "status: Active\nepistemic-status: seed\ncategories: [\"Uncategorized\"]\n"
        "updated: '2026-09-12'\nsources: []\n---\n见 [[Source_briefing-alpha]].\n",
    )
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_operations_for("Source_briefing-alpha.md", projection_hash)],
    )
    with pytest.raises(ValueError, match="still linked"):
        delete_wiki_batch(manifest, dry_run=True)


def test_refuses_system_page(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    projection_hash = _write_page(
        "System_Maintenance.md", _source_page_markdown("Maintenance", "body")
    )
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_operations_for("System_Maintenance.md", projection_hash)],
    )
    with pytest.raises(SystemPageDeleteNotAuthorized):
        delete_wiki_batch(manifest, dry_run=True)


def test_refuses_repeated_and_oversized_batches(tmp_path):
    repeated = _manifest(
        tmp_path / "repeated.json",
        [
            _operations_for("Source_briefing-alpha.md", "a" * 64),
            _operations_for("Source_briefing-alpha.md", "a" * 64),
        ],
    )
    with pytest.raises(ValueError):
        delete_wiki_batch(repeated, dry_run=True)
    oversized = _manifest(
        tmp_path / "oversized.json",
        [
            _operations_for(f"Source_briefing-{index:03d}.md", "a" * 64)
            for index in range(51)
        ],
    )
    with pytest.raises(ValueError, match="operation count exceeds"):
        delete_wiki_batch(oversized, dry_run=True)


def test_preview_fingerprint_changes_with_content(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    filename = "Source_briefing-alpha.md"
    projection_hash = _write_page(filename, _source_page_markdown("Alpha", "body"))
    manifest = _manifest(
        tmp_path / "manifest.json", [_operations_for(filename, projection_hash)]
    )
    first = delete_wiki_batch(manifest, dry_run=True)
    assert first["committed"] is False
    assert first["fingerprint"].startswith("sha256:")

    new_hash = _write_page(filename, _source_page_markdown("Alpha", "body changed"))
    manifest = _manifest(
        tmp_path / "manifest.json", [_operations_for(filename, new_hash)]
    )
    second = delete_wiki_batch(manifest, dry_run=True)
    assert first["fingerprint"] != second["fingerprint"]


def test_confirmation_must_match_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    filename = "Source_briefing-alpha.md"
    projection_hash = _write_page(filename, _source_page_markdown("Alpha", "body"))
    manifest = _manifest(
        tmp_path / "manifest.json", [_operations_for(filename, projection_hash)]
    )
    with pytest.raises(ValueError, match="fingerprint"):
        delete_wiki_batch(manifest, dry_run=False, confirmation="sha256:wrong")


def test_apply_archives_page_and_builds_delete_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    captured: list[list[dict]] = []

    def fake_batch(mutations, **kwargs):
        captured.append(list(mutations))
        return {
            "committed": True,
            "outbox_ids": [7],
            "deferred": ["Source_briefing-alpha.md"],
            "post_commit_warnings": [],
        }

    monkeypatch.setattr(
        "vector_lake.mutation_coordinator.execute_mutation_batch", fake_batch
    )
    filename = "Source_briefing-alpha.md"
    projection_hash = _write_page(filename, _source_page_markdown("Alpha", "body"))
    manifest = _manifest(
        tmp_path / "manifest.json", [_operations_for(filename, projection_hash)]
    )
    preview = delete_wiki_batch(manifest, dry_run=True)
    receipt = delete_wiki_batch(
        manifest, dry_run=False, confirmation=preview["fingerprint"]
    )

    assert receipt["committed"] is True
    assert receipt["outbox_ids"] == [7]
    assert receipt["deferred"] == [filename]
    assert captured == [
        [
            {
                "filename": filename,
                "is_delete": True,
                "expected_version": "",
                "expected_projection_hash": projection_hash,
            }
        ]
    ]
    archived = get_wiki_dir() / ".archive" / filename
    assert archived.is_file()
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == projection_hash
    assert (get_wiki_dir() / filename).is_file()


def test_archive_false_skips_archive_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    monkeypatch.setattr(
        "vector_lake.mutation_coordinator.execute_mutation_batch",
        lambda mutations, **kwargs: {
            "committed": True,
            "outbox_ids": [1],
            "deferred": [],
            "post_commit_warnings": [],
        },
    )
    filename = "Source_briefing-alpha.md"
    projection_hash = _write_page(filename, _source_page_markdown("Alpha", "body"))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_operations_for(filename, projection_hash)],
        archive=False,
    )
    preview = delete_wiki_batch(manifest, dry_run=True)
    receipt = delete_wiki_batch(
        manifest, dry_run=False, confirmation=preview["fingerprint"]
    )
    assert receipt["archive_paths"] == {}
    assert not (get_wiki_dir() / ".archive" / filename).exists()


def test_real_batch_deletes_canonical_page_and_its_claims(tmp_path, monkeypatch):
    """End-to-end: the reviewed cascade removes the page and its own claims."""
    monkeypatch.setattr(
        "vector_lake.tool_projection.create_maintenance_backup",
        lambda label="maintenance": str(tmp_path / label),
    )
    filename = "Source_briefing-alpha.md"
    content = _source_page_markdown(
        "Alpha briefing",
        "## 已核验事实\n\n- 事实：Alpha 简报记录了一项可核验的供给变化。\n",
    )
    execute_mutation_batch(
        [
            {
                "filename": filename,
                "content": content,
                "expected_version": "",
                "expected_projection_hash": "",
            }
        ],
        validation_mode="schema",
        origin="test-wiki-delete-seed",
        return_details=True,
    )
    page_key = filename[:-3]
    versions = governance_store.canonical_page_versions({page_key})
    assert versions.get(page_key)
    connection = db_store.get_connection()
    claims_before = connection.execute(
        "SELECT COUNT(*) FROM claims WHERE json_extract(data_json, '$.locator.page_key') = ?",
        (page_key,),
    ).fetchone()[0]
    assert claims_before >= 1

    projection_hash = hashlib.sha256((get_wiki_dir() / filename).read_bytes()).hexdigest()
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_operations_for(filename, projection_hash, versions[page_key])],
    )
    preview = delete_wiki_batch(manifest, dry_run=True)
    receipt = delete_wiki_batch(
        manifest, dry_run=False, confirmation=preview["fingerprint"]
    )

    assert receipt["committed"] is True
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM entities "
            "WHERE json_extract(data_json, '$.page_key') = ?",
            (page_key,),
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM claims "
            "WHERE json_extract(data_json, '$.locator.page_key') = ?",
            (page_key,),
        ).fetchone()[0]
        == 0
    )
