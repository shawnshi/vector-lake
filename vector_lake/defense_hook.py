import re
import json
from pathlib import Path
from vector_lake.schema_validator import validate_schema, SchemaViolationException
from vector_lake.purpose_contract import validate_ingest_payload, load_purpose_contract, PurposeContractError

class DefenseHookException(Exception):
    pass

def verify_asset(content: str, filename: str, frontmatter: dict, index_path: Path):
    """
    Adapter for the legacy Defense Hook to route everything through the new strict SchemaValidator.
    """
    try:
        validate_schema(frontmatter, content, filename, index_path)
        
        # Unified PurposeGate: Enforce strategic scope and evidence tier on all wiki content
        if filename.endswith(".md") and not filename.startswith("System_") and not filename.startswith("Orphan_"):
            contract = load_purpose_contract()
            item = {"filename": filename, "content": content}
            validate_ingest_payload([item], contract)
    except (SchemaViolationException, PurposeContractError) as e:
        raise DefenseHookException(str(e))
