import hashlib
import json
from pathlib import Path

from jsonschema.validators import Draft202012Validator
from vector_lake import db_store, governance_store, tool_projection
from vector_lake.claim_extractor import extract_page_objects


def _frontmatter(source_ref: str | None = None) -> dict:
    return {
        "id": "concept_foundation",
        "title": "Evidence Foundation",
        "type": "concept",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Uncategorized"],
        "created": "2026-07-21T00:00:00+00:00",
        "updated": "2026-07-21T00:00:00+00:00",
        "sources": [source_ref] if source_ref else [],
    }


def _content(body: str, source_ref: str | None = None) -> str:
    source_yaml = f"sources:\n  - {source_ref}\n" if source_ref else "sources: []\n"
    return (
        "---\n"
        "id: concept_foundation\n"
        "title: Evidence Foundation\n"
        "type: concept\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories: [Uncategorized]\n"
        "created: 2026-07-21T00:00:00+00:00\n"
        "updated: 2026-07-21T00:00:00+00:00\n"
        f"{source_yaml}"
        "---\n"
        "## 1. 编译事实\n"
        "### 物理机制 (Mechanism)\n"
        f"{body}\n\n"
        "## 2. 证据时间线\n"
    )


def test_real_source_bytes_and_raw_locator_are_preserved(isolated_memory):
    raw_path = isolated_memory / "raw" / "acceptance.txt"
    raw_path.write_bytes(b"signed acceptance")
    frontmatter = _frontmatter("raw/acceptance.txt")
    frontmatter["source_locators"] = {
        "raw/acceptance.txt": {"kind": "text", "paragraph": 3}
    }

    extracted = extract_page_objects(
        "Concept_Evidence-Foundation.md",
        frontmatter,
        "## 1. 编译事实\n### 物理机制 (Mechanism)\nMilestone M1 was accepted.\n\n## 2. 证据时间线\n",
    )

    source = extracted["sources"][0]
    evidence = extracted["evidence"][0]
    assert source["content_hash"] == hashlib.sha256(b"signed acceptance").hexdigest()
    assert source["integrity_status"] == "verified"
    assert source["byte_size"] == len(b"signed acceptance")
    assert evidence["source_locator"] == {"kind": "text", "paragraph": 3}
    assert evidence["projection_locator"]["page_key"] == "Concept_Evidence-Foundation"
    assert evidence["extraction_run_id"] == extracted["extraction_runs"][0]["run_id"]
    assert extracted["claims"][0]["extractor_version"] == "2.0"
    assert extracted["claims"][0]["confidence_kind"] == "legacy_prior"
    assert extracted["claims"][0]["calibrated_probability"] is None
    contract_dir = Path(__file__).resolve().parents[1] / "contracts" / "cbss"
    artifact_schema = json.loads(
        (contract_dir / "source-artifact.schema.json").read_text(encoding="utf-8")
    )
    run_schema = json.loads(
        (contract_dir / "extraction-run.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(artifact_schema).validate(extracted["source_artifacts"][0])
    Draft202012Validator(run_schema).validate(extracted["extraction_runs"][0])


def test_missing_source_stays_explicitly_unverified(isolated_memory):
    extracted = extract_page_objects(
        "Concept_Evidence-Foundation.md",
        _frontmatter("raw/missing.pdf"),
        "## 1. 编译事实\nA claim with a missing source.\n\n## 2. 证据时间线\n",
    )

    source = extracted["sources"][0]
    assert source["content_hash"] is None
    assert source["hash_algorithm"] is None
    assert source["integrity_status"] == "unverified"
    assert extracted["evidence"][0]["source_locator"] == {
        "kind": "unresolved",
        "raw_ref": "raw/missing.pdf",
    }


def test_verified_source_without_segment_metadata_uses_artifact_locator(isolated_memory):
    raw_path = isolated_memory / "raw" / "whole.txt"
    raw_path.write_text("whole artifact", encoding="utf-8")

    extracted = extract_page_objects(
        "Concept_Evidence-Foundation.md",
        _frontmatter("raw/whole.txt"),
        "## 1. 编译事实\nWhole artifact claim.\n\n## 2. 证据时间线\n",
    )

    artifact = extracted["source_artifacts"][0]
    locator = extracted["evidence"][0]["source_locator"]
    assert locator == {
        "kind": "artifact",
        "scope": "whole-artifact",
        "raw_ref": "raw/whole.txt",
        "artifact_id": artifact["artifact_id"],
        "sha256": artifact["sha256"],
    }


def test_change_application_keeps_append_only_claim_and_evidence_versions(isolated_memory):
    raw_path = isolated_memory / "raw" / "source.txt"
    raw_path.write_text("source", encoding="utf-8")
    first = governance_store.prepare_change_set_from_content(
        "Concept_Evidence-Foundation.md",
        _content("Version one.", "raw/source.txt"),
        "test",
    )
    governance_store.apply_change_set(first)
    second = governance_store.prepare_change_set_from_content(
        "Concept_Evidence-Foundation.md",
        _content("Version two.", "raw/source.txt"),
        "test",
    )
    governance_store.apply_change_set(second)

    conn = db_store.get_connection()
    claim_rows = conn.execute(
        "SELECT version_no, data_json FROM claim_versions "
        "WHERE page_key = 'Concept_Evidence-Foundation' ORDER BY version_no"
    ).fetchall()
    evidence_rows = conn.execute(
        "SELECT record_hash FROM evidence_versions "
        "WHERE page_key = 'Concept_Evidence-Foundation'"
    ).fetchall()
    current = [
        json.loads(row["data_json"])["claim_text"]
        for row in conn.execute(
            "SELECT data_json FROM claims "
            "WHERE json_extract(data_json, '$.locator.page_key') = 'Concept_Evidence-Foundation'"
        )
    ]

    versioned_texts = [json.loads(row["data_json"])["claim_text"] for row in claim_rows]
    assert "Version one." in versioned_texts
    assert "Version two." in versioned_texts
    assert "Version one." not in current
    assert "Version two." in current
    assert len({row["record_hash"] for row in evidence_rows}) == len(evidence_rows)
    assert conn.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 2


def test_source_reingest_preserves_durable_metadata_while_refreshing_artifact(
    isolated_memory,
):
    raw_path = isolated_memory / "raw" / "source.txt"
    raw_path.write_text("source-v1", encoding="utf-8")
    first = governance_store.prepare_change_set_from_content(
        "Concept_Evidence-Foundation.md",
        _content("Version one.", "raw/source.txt"),
        "test",
    )
    governance_store.apply_change_set(first)

    conn = db_store.get_connection()
    source_row = conn.execute("SELECT source_id, data_json FROM sources").fetchone()
    source = json.loads(source_row["data_json"])
    source.update(
        {
            "ingested_at": "2026-01-01T00:00:00+00:00",
            "classification": "restricted",
            "retention_policy": "retain-7-years",
            "legal_hold": True,
            "legacy_content_hash": "legacy-reviewed-hash",
            "reviewed_provenance": {"reviewer": "human-board"},
            "generation_parent_refs": ["raw/parent-a.md"],
        }
    )
    artifact_row = conn.execute(
        "SELECT artifact_id, data_json FROM source_artifacts"
    ).fetchone()
    artifact = json.loads(artifact_row["data_json"])
    artifact.update(
        {
            "classification": "restricted",
            "retention_policy": "retain-7-years",
            "legal_hold": True,
            "reviewed_provenance": {"reviewer": "human-board"},
            "generation_parent_refs": ["raw/parent-a.md"],
        }
    )
    with db_store.transaction():
        conn.execute(
            "UPDATE sources SET data_json = ? WHERE source_id = ?",
            (
                json.dumps(source, ensure_ascii=False),
                source_row["source_id"],
            ),
        )
        conn.execute(
            "UPDATE source_artifacts SET data_json = ? WHERE artifact_id = ?",
            (
                json.dumps(artifact, ensure_ascii=False),
                artifact_row["artifact_id"],
            ),
        )

    second = governance_store.prepare_change_set_from_content(
        "Concept_Evidence-Foundation.md",
        _content("Version two.", "raw/source.txt"),
        "test",
    )
    governance_store.apply_change_set(second)
    reingested_artifact = json.loads(
        conn.execute(
            "SELECT data_json FROM source_artifacts WHERE artifact_id = ?",
            (artifact_row["artifact_id"],),
        ).fetchone()["data_json"]
    )
    assert reingested_artifact["reviewed_provenance"] == {
        "reviewer": "human-board"
    }
    assert reingested_artifact["classification"] == "restricted"
    assert reingested_artifact["retention_policy"] == "retain-7-years"
    assert reingested_artifact["legal_hold"] is True
    assert reingested_artifact["generation_parent_refs"] == ["raw/parent-a.md"]

    raw_path.write_text("source-v2-with-new-bytes", encoding="utf-8")
    third = governance_store.prepare_change_set_from_content(
        "Concept_Evidence-Foundation.md",
        _content("Version three.", "raw/source.txt"),
        "test",
    )
    governance_store.apply_change_set(third)

    expected_hash = hashlib.sha256(b"source-v2-with-new-bytes").hexdigest()
    refreshed_source = json.loads(
        conn.execute(
            "SELECT data_json FROM sources WHERE source_id = ?",
            (source_row["source_id"],),
        ).fetchone()["data_json"]
    )
    refreshed_artifact_row = conn.execute(
        "SELECT sha256, byte_size, data_json FROM source_artifacts WHERE artifact_id = ?",
        (refreshed_source["artifact_id"],),
    ).fetchone()
    assert refreshed_source["content_hash"] == expected_hash
    assert refreshed_source["ingested_at"] == "2026-01-01T00:00:00+00:00"
    assert refreshed_source["legacy_content_hash"] == "legacy-reviewed-hash"
    assert refreshed_source["classification"] == "restricted"
    assert refreshed_source["retention_policy"] == "retain-7-years"
    assert refreshed_source["legal_hold"] is True
    assert refreshed_source["reviewed_provenance"] == {"reviewer": "human-board"}
    assert refreshed_source["generation_parent_refs"] == ["raw/parent-a.md"]
    assert refreshed_artifact_row["sha256"] == expected_hash
    assert refreshed_artifact_row["byte_size"] == len(b"source-v2-with-new-bytes")


def test_entity_identity_registry_tracks_explicit_identity(isolated_memory):
    db_store.init_db()
    content = _content("Stable identity.")
    content = content.replace(
        "title: Evidence Foundation\n",
        "title: Evidence Foundation\nentity_id: entity_business_123\n",
    )
    change_set = governance_store.prepare_change_set_from_content(
        "Concept_Evidence-Foundation.md", content, "test"
    )
    governance_store.apply_change_set(change_set)

    row = db_store.get_connection().execute(
        "SELECT entity_id, page_key, identity_origin FROM entity_identities"
    ).fetchone()
    assert dict(row) == {
        "entity_id": "entity_business_123",
        "page_key": "Concept_Evidence-Foundation",
        "identity_origin": "explicit",
    }


def test_foundation_backfill_is_merge_only_and_resumable(isolated_memory):
    raw_path = isolated_memory / "raw" / "source.txt"
    raw_path.write_text("source", encoding="utf-8")
    page_path = isolated_memory / "wiki" / "Concept_Evidence-Foundation.md"
    content = _content("Stable reviewed claim.", "raw/source.txt")
    page_path.write_text(content, encoding="utf-8")
    governance_store.apply_change_set(
        governance_store.prepare_change_set_from_content(page_path.name, content, "test")
    )

    conn = db_store.get_connection()
    claim_row = conn.execute("SELECT claim_id, data_json FROM claims LIMIT 1").fetchone()
    claim = json.loads(claim_row["data_json"])
    for field in (
        "claim_family_id",
        "confidence_kind",
        "extractor_name",
        "extractor_version",
        "extraction_run_id",
    ):
        claim.pop(field, None)
    claim["assessment_status"] = "human_accepted"
    claim["calibrated_probability"] = 0.91

    evidence_row = conn.execute("SELECT evidence_id, data_json FROM evidence LIMIT 1").fetchone()
    evidence = json.loads(evidence_row["data_json"])
    for field in governance_store._EVIDENCE_FOUNDATION_FIELDS:
        evidence.pop(field, None)

    source_row = conn.execute("SELECT source_id, data_json FROM sources LIMIT 1").fetchone()
    source = json.loads(source_row["data_json"])
    for field in governance_store._SOURCE_FOUNDATION_FIELDS:
        source.pop(field, None)
    source["content_hash"] = "legacy-hash"

    with db_store.transaction():
        conn.execute(
            "UPDATE claims SET data_json = ? WHERE claim_id = ?",
            (json.dumps(claim, ensure_ascii=False), claim_row["claim_id"]),
        )
        conn.execute(
            "UPDATE evidence SET data_json = ? WHERE evidence_id = ?",
            (json.dumps(evidence, ensure_ascii=False), evidence_row["evidence_id"]),
        )
        conn.execute(
            "UPDATE sources SET data_json = ? WHERE source_id = ?",
            (json.dumps(source, ensure_ascii=False), source_row["source_id"]),
        )
        conn.execute("DELETE FROM source_artifacts")
        conn.execute("DELETE FROM extraction_runs")
        conn.execute("DELETE FROM entity_identities")
        conn.execute("DELETE FROM claim_versions")
        conn.execute("DELETE FROM evidence_versions")

    preview = tool_projection.evidence_foundation_backfill(dry_run=True, limit=10)
    assert "pending_pages: 1" in preview
    result = tool_projection.evidence_foundation_backfill(dry_run=False, limit=10)
    assert "Backfilled 1 page revision(s)" in result

    current_claim = json.loads(
        conn.execute("SELECT data_json FROM claims WHERE claim_id = ?", (claim_row["claim_id"],)).fetchone()[0]
    )
    current_evidence = json.loads(
        conn.execute("SELECT data_json FROM evidence WHERE evidence_id = ?", (evidence_row["evidence_id"],)).fetchone()[0]
    )
    current_source = json.loads(
        conn.execute("SELECT data_json FROM sources WHERE source_id = ?", (source_row["source_id"],)).fetchone()[0]
    )
    assert current_claim["assessment_status"] == "human_accepted"
    assert current_claim["calibrated_probability"] == 0.91
    assert current_claim["extractor_name"] == "vector_lake.foundation_backfill"
    assert current_claim["extractor_version"] == "1.0"
    assert current_evidence["source_locator"]["kind"] == "artifact"
    assert current_evidence["source_locator"]["scope"] == "whole-artifact"
    assert current_evidence["source_locator"]["raw_ref"] == "raw/source.txt"
    assert len(current_evidence["source_locator"]["sha256"]) == 64
    assert current_source["legacy_content_hash"] == "legacy-hash"
    assert current_source["content_hash"] == hashlib.sha256(b"source").hexdigest()
    assert current_source["integrity_status"] == "verified"
    assert conn.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM entity_identities").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0] == 2

    second = tool_projection.evidence_foundation_backfill(dry_run=False, limit=10)
    assert second == "No pending evidence-foundation page revisions to backfill."


def test_foundation_backfill_repairs_canonical_only_lineage_without_inventing_run(
    isolated_memory,
):
    raw_path = isolated_memory / "raw" / "canonical-only.txt"
    raw_path.write_text("canonical only source", encoding="utf-8")
    content = _content("Canonical-only claim.", "raw/canonical-only.txt")
    change_set = governance_store.prepare_change_set_from_content(
        "Concept_Canonical-Only.md",
        content.replace("id: concept_foundation", "id: concept_canonical_only").replace(
            "title: Evidence Foundation", "title: Canonical Only"
        ),
        "test",
    )
    governance_store.apply_change_set(change_set)
    conn = db_store.get_connection()
    evidence_row = conn.execute(
        "SELECT evidence_id, data_json FROM evidence LIMIT 1"
    ).fetchone()
    evidence = json.loads(evidence_row["data_json"])
    for field in ("artifact_id", "source_locator", "independence_status", "lineage_safe"):
        evidence.pop(field, None)
    source_row = conn.execute("SELECT source_id, data_json FROM sources LIMIT 1").fetchone()
    source = json.loads(source_row["data_json"])
    for field in governance_store._SOURCE_FOUNDATION_FIELDS:
        source.pop(field, None)
    with db_store.transaction():
        conn.execute(
            "UPDATE evidence SET data_json = ? WHERE evidence_id = ?",
            (json.dumps(evidence, ensure_ascii=False), evidence_row["evidence_id"]),
        )
        conn.execute(
            "UPDATE sources SET data_json = ? WHERE source_id = ?",
            (json.dumps(source, ensure_ascii=False), source_row["source_id"]),
        )
        conn.execute("DELETE FROM source_artifacts")
        conn.execute("DELETE FROM extraction_runs")
    assert not (isolated_memory / "wiki" / "Concept_Canonical-Only.md").exists()

    preview = tool_projection.evidence_foundation_backfill(dry_run=True, limit=10)
    assert "pending_pages: 1" in preview
    result = tool_projection.evidence_foundation_backfill(dry_run=False, limit=10)
    assert "Backfilled 1 page revision(s)" in result

    repaired_evidence = json.loads(
        conn.execute(
            "SELECT data_json FROM evidence WHERE evidence_id = ?",
            (evidence_row["evidence_id"],),
        ).fetchone()[0]
    )
    repaired_source = json.loads(
        conn.execute(
            "SELECT data_json FROM sources WHERE source_id = ?",
            (source_row["source_id"],),
        ).fetchone()[0]
    )
    assert repaired_evidence["source_locator"]["kind"] == "artifact"
    assert repaired_evidence["lineage_safe"] is True
    assert repaired_source["integrity_status"] == "verified"
    assert len(repaired_source["content_hash"]) == 64
    assert conn.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 0
