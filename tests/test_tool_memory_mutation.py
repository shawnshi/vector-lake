from vector_lake import db_store
from vector_lake.schema_validator import validate_schema
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
    assert "[Observation]" in body
    conn = db_store.get_connection()
    assert conn.execute(
        "SELECT 1 FROM entities WHERE json_extract(data_json, '$.page_key') = 'Concept_OperationalFacts'"
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM mutation_outbox WHERE filename = 'Concept_OperationalFacts.md' AND status = 'pending'"
    ).fetchone() is not None
