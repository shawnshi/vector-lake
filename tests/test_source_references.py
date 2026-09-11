from vector_lake.claim_extractor import _stable_id, extract_page_objects
from vector_lake.source_references import (
    canonical_source_page_for_ref,
    normalize_explicit_source_page_ref,
    source_id_for_raw_ref,
)


def _frontmatter(*, page_type="concept", sources=None):
    return {
        "id": "source_mapping_fixture",
        "title": "Source Mapping Fixture",
        "type": page_type,
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Uncategorized"],
        "updated": "2026-09-09",
        "sources": sources or [],
    }


def test_explicit_source_refs_normalize_once_and_reject_unsafe_forms():
    forms = ["Source_X", "Source_X.md", "[[Source_X]]", "[[Source_X|label]]"]
    assert [normalize_explicit_source_page_ref(value) for value in forms] == ["Source_X"] * 4
    assert normalize_explicit_source_page_ref("Source_Source_X") == "Source_Source_X"
    assert normalize_explicit_source_page_ref("[[Source_X]]") == normalize_explicit_source_page_ref("Source_X")
    assert normalize_explicit_source_page_ref("[[Source_X/../../secret]]") == ""
    assert normalize_explicit_source_page_ref("raw/Source_X.md") == ""


def test_extraction_maps_only_explicit_refs_without_rekeying_identity():
    refs = ["Source_X", "Source_X.md", "raw/a/report.pdf", "raw/b/report.pdf"]
    result = extract_page_objects(
        "Concept_Source_Map.md",
        _frontmatter(sources=refs),
        "## 1. 编译事实\n\nA stable claim.\n\n## 2. 证据时间线\n",
    )
    by_ref = {record["raw_ref"]: record for record in result["sources"]}
    assert by_ref["Source_X"]["canonical_source_page"] == "Source_X.md"
    assert by_ref["Source_X.md"]["canonical_source_page"] == "Source_X.md"
    assert by_ref["raw/a/report.pdf"]["canonical_source_page"] == ""
    assert by_ref["raw/b/report.pdf"]["canonical_source_page"] == ""
    for raw_ref in refs:
        assert by_ref[raw_ref]["source_id"] == _stable_id("source", raw_ref)
        assert by_ref[raw_ref]["source_id"] == source_id_for_raw_ref(raw_ref)
        assert by_ref[raw_ref]["artifact_id"]


def test_source_page_retains_its_actual_page_name_for_raw_artifact():
    result = extract_page_objects(
        "Source_Actual_Source_Source.md",
        _frontmatter(page_type="source", sources=["raw/report.pdf"]),
        "Primary source content.",
    )
    source = result["sources"][0]
    assert source["canonical_source_page"] == "Source_Actual_Source_Source.md"
    assert canonical_source_page_for_ref("raw/report.pdf") == ""
