from __future__ import annotations

import json
from pathlib import Path


CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "cbss"


def _load(name: str) -> dict:
    return json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))


def test_all_cbss_schemas_are_json_schema_objects() -> None:
    schemas = sorted(CONTRACT_DIR.glob("*.schema.json"))
    assert [path.name for path in schemas] == [
        "business-event-envelope.schema.json",
        "claim-acceptance-record.schema.json",
        "claim-assessment.schema.json",
        "critical-decision-registry.schema.json",
        "evidence-packet.schema.json",
        "extraction-run.schema.json",
        "semantic-readiness.schema.json",
        "source-artifact.schema.json",
    ]
    for path in schemas:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert payload["type"] == "object"


def test_claim_acceptance_requires_explicit_authority_disposition() -> None:
    schema = _load("claim-acceptance-record.schema.json")
    assert schema["properties"]["disposition"]["enum"] == [
        "accepted",
        "rejected",
        "deferred",
        "revoked",
    ]
    assert schema["properties"]["authority"]["required"] == [
        "actor_id",
        "role",
        "scope",
    ]


def test_business_event_contract_has_temporal_and_causal_fields() -> None:
    schema = _load("business-event-envelope.schema.json")
    required = set(schema["required"])
    assert {
        "event_sequence",
        "valid_time",
        "recorded_time",
        "causation_id",
        "correlation_id",
    } <= required
    assert schema["properties"]["event_sequence"] == {
        "type": "integer",
        "minimum": 1,
    }


def test_semantic_readiness_contract_is_not_binary_health_alias() -> None:
    schema = _load("semantic-readiness.schema.json")
    assert schema["properties"]["status"]["enum"] == [
        "ready",
        "degraded",
        "not_ready",
    ]
    assert {"issues", "warnings", "detail"} <= set(schema["required"])
