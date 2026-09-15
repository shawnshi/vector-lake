from vector_lake.schema_validator import validate_schema, SchemaViolationException
from vector_lake.purpose_contract import validate_ingest_payload, load_purpose_contract, PurposeContractError

class DefenseHookException(Exception):
    pass

def verify_asset(content: str, filename: str, frontmatter: dict, index_entities=None):
    """Adapter for the legacy Defense Hook to route everything through the new strict SchemaValidator.

    ``index_entities`` is a callable returning the index's titles and aliases, or
    ``None`` to skip the tag-collision check. It used to be an index path, which
    made the validator read the index itself.
    """
    try:
        validate_schema(frontmatter, content, filename, index_entities)
        
        # Unified PurposeGate: Enforce strategic scope and evidence tier on all wiki content
        if filename.casefold().endswith(".md") and not filename.startswith("System_") and not filename.startswith("Orphan_"):
            contract = load_purpose_contract()
            item = {"filename": filename, "content": content}
            validate_ingest_payload([item], contract)
    except (SchemaViolationException, PurposeContractError) as e:
        raise DefenseHookException(str(e))
