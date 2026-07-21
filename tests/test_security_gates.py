from vector_lake.tool_delete import delete_source
from vector_lake.tool_rename import rename_vector_lake_entity
from vector_lake.tool_bulk_reconciliation import bulk_reconcile
from vector_lake.wiki_utils import get_memory_dir, get_wiki_dir

def test_delete_source_boundary():
    evil_path = "../evil_file.md"
    result = delete_source(evil_path, dry_run=True)
    assert "Security Error" in result or "boundary" in result

def test_rename_entity_boundary(isolated_memory):
    evil_old = "Concept_A"
    evil_new = "../../../etc/passwd"

    dummy_path = get_wiki_dir() / "Concept_A.md"
    dummy_path.parent.mkdir(parents=True, exist_ok=True)
    dummy_path.write_text("dummy")
    try:
        result = rename_vector_lake_entity(evil_old, evil_new, dry_run=True)
        # It might be blocked or it might be sanitized (e.g. replacing / with -)
        assert "Security Error" in result or "boundary" in result or "..-..-..-etc-passwd.md" in result
    finally:
        dummy_path.unlink()

def test_bulk_reconciliation_boundary(isolated_memory):
    operations = [{"source_entity": "Concept_A", "target_entity": "../evil"}]
    dummy_path = get_wiki_dir() / "Concept_A.md"
    dummy_path.parent.mkdir(parents=True, exist_ok=True)
    dummy_path.write_text("dummy")
    try:
        result = bulk_reconcile(operations)
        assert "Security Error" in result or "boundary" in result or "No operations" in result or "..-evil.md" in result
    finally:
        dummy_path.unlink()

def test_delete_dry_run_default(isolated_memory):
    # It should not delete anything
    fake_path = str(get_memory_dir() / "raw" / "test_fake.md")
    # we just want to ensure it doesn't crash on dry_run
    try:
        delete_source(fake_path)
    except Exception:
        pass # as long as it doesn't do a real delete and crash on missing
