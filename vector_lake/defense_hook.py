import re
import json
from pathlib import Path
from vector_lake.schema_validator import validate_schema, SchemaViolationException

class DefenseHookException(Exception):
    pass

def verify_asset(content: str, filename: str, frontmatter: dict, index_path: Path):
    """
    Adapter for the legacy Defense Hook to route everything through the new strict SchemaValidator.
    """
    try:
        validate_schema(frontmatter, content, filename, index_path)
    except SchemaViolationException as e:
        raise DefenseHookException(str(e))
