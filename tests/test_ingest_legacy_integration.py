import hashlib
import json

import pytest

from vector_lake import db_store, governance_store
from vector_lake.mutation_coordinator import (
    execute_mutation_batch,
    execute_mutation_plan,
)
from vector_lake.tool_ingest import claim_ingest_tasks, finalize_ingest
from vector_lake.wiki_utils import split_frontmatter
from tests.test_ingest_contract import (
    _claimed_processed_data,
    _concept_content,
    _integration_candidate,
    _v4_ingest_payload,
)
from tests.test_mutation_coordinator import (
    _source_content,
    _write_purpose_contract,
)


def _seed_legacy_target(isolated_memory, *, colliding_tag=None):
    target_name = "Concept_Legacy-Target.md"
    legacy_content = (
        _concept_content("Legacy Target")
        .replace("strategic_scope: core\n", "")
        .replace("evidence_tier: primary\n", "")
    )
    if colliding_tag:
        legacy_content = legacy_content.replace(
            "categories: [System_Architecture]\n",
            f"categories: [System_Architecture]\ntags: [{colliding_tag}]\n",
        )
    execute_mutation_batch(
        [{"filename": target_name, "content": legacy_content}],
        validation_mode="schema",
    )
    target_path = isolated_memory / "wiki" / target_name
    target_version = governance_store.canonical_page_versions(
        {"Concept_Legacy-Target"}
    )["Concept_Legacy-Target"]
    projection_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
    return target_name, target_path, target_version, projection_hash


def _integrated_job(target_name, target_version, projection_hash):
    payload = _v4_ingest_payload(
        "raw/legacy-integration.md",
        "legacy-integration-hash",
        "Source_Legacy-Integration.md",
        integration_candidates=[
            _integration_candidate(
                target_name,
                target_version,
                projection_hash,
            )
        ],
    )
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = __import__("json").loads(
        claim_ingest_tasks(limit=1, lease_seconds=60)
    )[0]
    processed_data = _claimed_processed_data(
        payload,
        job_id,
        claim,
        integration={
            "disposition": "integrated",
            "relations": [
                {
                    "target": target_name,
                    "target_hash": target_version,
                    "target_projection_hash": projection_hash,
                    "predicate": "validates",
                    "evidence": (
                        "The source directly validates the legacy target mechanism."
                    ),
                    "confidence": 0.93,
                    "event_date": "2026-07-28",
                    "event_tag": "Validation",
                }
            ],
        },
    )
    return job_id, processed_data


def test_integrated_ingest_preserves_legacy_metadata_and_records_mixed_modes(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    target_name, target_path, target_version, projection_hash = (
        _seed_legacy_target(isolated_memory)
    )
    outbox_before = db_store.get_connection().execute(
        "SELECT COALESCE(MAX(id), 0) FROM mutation_outbox"
    ).fetchone()[0]
    job_id, processed_data = _integrated_job(
        target_name,
        target_version,
        projection_hash,
    )

    result = finalize_ingest(
        [
            {
                "filename": "Source_Legacy-Integration.md",
                "content": _source_content(),
            }
        ],
        processed_data,
    )

    assert result.startswith("Successfully finalized ingestion")
    source_path = isolated_memory / "wiki" / "Source_Legacy-Integration.md"
    assert source_path.exists()
    target_content = target_path.read_text(encoding="utf-8")
    target_frontmatter, _body = split_frontmatter(target_content)
    assert "evidence_tier" not in target_frontmatter
    assert "strategic_scope" not in target_frontmatter
    assert "(Source: [[Source_Legacy-Integration]])" in target_content
    rows = db_store.get_connection().execute(
        "SELECT filename, validation_mode FROM mutation_outbox "
        "WHERE id > ? ORDER BY id",
        (outbox_before,),
    ).fetchall()
    assert {row["filename"]: row["validation_mode"] for row in rows} == {
        "Source_Legacy-Integration.md": "full",
        target_name: "schema",
    }
    assert (
        db_store.get_connection()
        .execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        .fetchone()[0]
        == "finalized"
    )


def test_integrated_ingest_preserves_legacy_dynamic_tag_debt(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    target_name, target_path, target_version, projection_hash = (
        _seed_legacy_target(isolated_memory, colliding_tag="Agentic_AI")
    )
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "Concept_Agentic-AI": {
                        "title": "Agentic_AI",
                        "aliases": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _job_id, processed_data = _integrated_job(
        target_name,
        target_version,
        projection_hash,
    )

    result = finalize_ingest(
        [
            {
                "filename": "Source_Legacy-Integration.md",
                "content": _source_content(),
            }
        ],
        processed_data,
    )

    assert result.startswith("Successfully finalized ingestion")
    target_content = target_path.read_text(encoding="utf-8")
    target_frontmatter, _body = split_frontmatter(target_content)
    assert target_frontmatter["tags"] == ["Agentic_AI"]
    latest_target_outbox = db_store.get_connection().execute(
        "SELECT validation_mode FROM mutation_outbox WHERE filename = ? "
        "ORDER BY id DESC LIMIT 1",
        (target_name,),
    ).fetchone()
    assert latest_target_outbox["validation_mode"] == "schema"

def test_integrated_ingest_keeps_current_target_on_full_validation(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    target_name = "Concept_Current-Target.md"
    target_path = isolated_memory / "wiki" / target_name
    execute_mutation_plan(target_name, content=_concept_content("Current Target"))
    target_version = governance_store.canonical_page_versions(
        {"Concept_Current-Target"}
    )["Concept_Current-Target"]
    projection_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
    outbox_before = db_store.get_connection().execute(
        "SELECT COALESCE(MAX(id), 0) FROM mutation_outbox"
    ).fetchone()[0]
    _job_id, processed_data = _integrated_job(
        target_name,
        target_version,
        projection_hash,
    )

    result = finalize_ingest(
        [
            {
                "filename": "Source_Legacy-Integration.md",
                "content": _source_content(),
            }
        ],
        processed_data,
    )

    assert result.startswith("Successfully finalized ingestion")
    rows = db_store.get_connection().execute(
        "SELECT filename, validation_mode FROM mutation_outbox "
        "WHERE id > ? ORDER BY id",
        (outbox_before,),
    ).fetchall()
    assert {row["filename"]: row["validation_mode"] for row in rows} == {
        "Source_Legacy-Integration.md": "full",
        target_name: "full",
    }


def test_integrated_ingest_does_not_downgrade_invalid_submitted_source(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    target_name, target_path, target_version, projection_hash = (
        _seed_legacy_target(isolated_memory)
    )
    target_before = target_path.read_bytes()
    outbox_before = db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0]
    job_id, processed_data = _integrated_job(
        target_name,
        target_version,
        projection_hash,
    )
    invalid_source = _source_content().replace(
        "evidence_tier: primary\n",
        "",
    )

    result = finalize_ingest(
        [
            {
                "filename": "Source_Legacy-Integration.md",
                "content": invalid_source,
            }
        ],
        processed_data,
    )

    assert result.startswith("Error finalizing ingestion")
    assert "evidence_tier" in result
    assert target_path.read_bytes() == target_before
    assert not (isolated_memory / "wiki" / "Source_Legacy-Integration.md").exists()
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM mutation_outbox")
        .fetchone()[0]
        == outbox_before
    )
    assert (
        db_store.get_connection()
        .execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        .fetchone()[0]
        == "subagent_processing"
    )


def test_schema_maintenance_exception_requires_fenced_existing_target(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    target_name, target_path, target_version, projection_hash = (
        _seed_legacy_target(isolated_memory)
    )
    content = target_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="canonical and projection baselines"):
        execute_mutation_batch(
            [{"filename": target_name, "content": content}],
            schema_maintenance_filenames={target_name},
        )

    source_name = "Source_Existing.md"
    execute_mutation_plan(source_name, content=_source_content())
    source_path = isolated_memory / "wiki" / source_name
    source_version = governance_store.canonical_page_versions(
        {"Source_Existing"}
    )["Source_Existing"]
    source_projection = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="existing non-Source update"):
        execute_mutation_batch(
            [
                {
                    "filename": source_name,
                    "content": source_path.read_text(encoding="utf-8"),
                    "expected_version": source_version,
                    "expected_projection_hash": source_projection,
                }
            ],
            schema_maintenance_filenames={source_name},
        )

    with pytest.raises(ValueError, match="absent from the mutation batch"):
        execute_mutation_batch(
            [
                {
                    "filename": target_name,
                    "content": _concept_content("Legacy Target"),
                    "expected_version": target_version,
                    "expected_projection_hash": projection_hash,
                }
            ],
            schema_maintenance_filenames={"Concept_Other.md"},
        )
