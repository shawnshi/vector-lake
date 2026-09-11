import json

import pytest

from vector_lake import db_store
from vector_lake import governance_store
from vector_lake.governance_store import _memory_object_from_claim
from vector_lake.schema_validator import validate_schema
from vector_lake.tool_search import build_memory_packet
from vector_lake.tool_memory import update_operational_memory
from vector_lake.wiki_utils import split_frontmatter

from tests.test_mutation_coordinator import _write_purpose_contract


def test_operational_memory_uses_valid_schema_and_mutation_coordinator(isolated_memory):
    _write_purpose_contract(isolated_memory)

    result = update_operational_memory("fact", "The durable outbox is polled without a signal file.")

    assert "canonical state and outbox intent committed" in result
    path = isolated_memory / "wiki" / "Concept_OperationalFacts.md"
    content = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(content)
    validate_schema(frontmatter, body, path.name)
    assert frontmatter["id"] == "operational-memory-fact"
    assert frontmatter["status"] == "Active"
    assert frontmatter["authoring_origin"] == "governed_operational_memory"
    assert "[Observation]" in body
    conn = db_store.get_connection()
    assert conn.execute(
        "SELECT 1 FROM entities WHERE json_extract(data_json, '$.page_key') = 'Concept_OperationalFacts'"
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM mutation_outbox WHERE filename = 'Concept_OperationalFacts.md' AND status = 'pending'"
    ).fetchone() is not None


@pytest.mark.parametrize(
    ("memory_type", "filename", "section"),
    [
        ("preference", "Concept_UserPreferences.md", "Current Preferences"),
        ("decision", "Concept_SystemDecisions.md", "Open Decisions"),
        ("task_state", "Concept_AgentTaskState.md", "Task State"),
        ("fact", "Concept_OperationalFacts.md", "Relevant Facts"),
    ],
)
def test_actual_update_builds_typed_canonical_memory_packet(
    isolated_memory, monkeypatch, memory_type, filename, section
):
    _write_purpose_contract(isolated_memory)
    unique = f"batch2b-{memory_type}-unique-content"

    result = update_operational_memory(memory_type, unique)
    assert "canonical state and outbox intent committed" in result

    conn = db_store.get_connection()
    rows = conn.execute("SELECT data_json FROM claims").fetchall()
    claims = [json.loads(row[0]) for row in rows]
    matches = [
        claim for claim in claims
        if unique in claim.get("claim_text", "")
        and claim.get("claim_type") == "timeline-event"
    ]
    assert len(matches) == 1
    claim = matches[0]
    assert claim["source_page"] == filename
    assert claim["memory_type"] == memory_type
    assert claim["operational_memory_provenance"] is True

    memory = _memory_object_from_claim(claim)
    assert memory["memory_type"] == memory_type
    assert memory["source_claim_id"] == claim["claim_id"]
    assert memory["source_ids"] == claim["source_ids"]
    assert memory["evidence_ids"] == claim["evidence_ids"]

    monkeypatch.setattr(
        governance_store,
        "search_operational_memory_views",
        lambda *_args, **_kwargs: ([memory], []),
    )
    packet = build_memory_packet("operational memory routing")["packet"]
    expected_section = packet.split(f"## {section}", 1)[1].split("## ", 1)[0]
    assert unique in expected_section
    assert packet.count(unique) == 1
