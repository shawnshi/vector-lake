import pytest

from vector_lake import db_store
from vector_lake.quality_registry import register_schema, record_quality_evaluation


def test_schema_versions_are_immutable(isolated_memory):
    first = register_schema(
        "claim-dialect",
        "1.0",
        dialect_id="vector-lake-claim",
        schema={"type": "object", "required": ["claim_id"]},
    )
    replay = register_schema(
        "claim-dialect",
        "1.0",
        dialect_id="vector-lake-claim",
        schema={"type": "object", "required": ["claim_id"]},
    )

    assert first["schema_hash"] == replay["schema_hash"]
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM schema_registry"
    ).fetchone()[0] == 1
    with pytest.raises(ValueError, match="Schema version is immutable"):
        register_schema(
            "claim-dialect",
            "1.0",
            dialect_id="vector-lake-claim",
            schema={"type": "object", "required": ["different"]},
        )


def test_quality_evaluation_records_threshold_failures_idempotently(isolated_memory):
    result = record_quality_evaluation(
        "golden-claims",
        "2026-07-21",
        "extractor-2.0",
        metrics={"precision": 0.96, "recall": 0.84},
        thresholds={"precision": 0.95, "recall": 0.90},
        sample_count=100,
    )
    replay = record_quality_evaluation(
        "golden-claims",
        "2026-07-21",
        "extractor-2.0",
        metrics={"precision": 0.96, "recall": 0.84},
        thresholds={"precision": 0.95, "recall": 0.90},
        sample_count=100,
    )

    assert result["status"] == "fail"
    assert result["failures"] == {"recall": {"actual": 0.84, "required": 0.90}}
    assert result["evaluation_id"] == replay["evaluation_id"]
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM quality_evaluation_runs"
    ).fetchone()[0] == 1
