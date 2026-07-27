from vector_lake import governance_store
from vector_lake.merge_analysis import build_wiki_backlink_index
from vector_lake.tool_governance_maintenance import _wiki_files
from vector_lake.tool_projection import _wiki_keys
from vector_lake.wiki_utils import iter_markdown_files


def test_markdown_iterator_and_core_scanners_include_uppercase_extension(
    isolated_memory,
):
    wiki_dir = isolated_memory / "wiki"
    (wiki_dir / "Source_Upper.MD").write_text(
        "[[Concept_Target.MD]]",
        encoding="utf-8",
    )
    (wiki_dir / "Concept_Target.md").write_text("target", encoding="utf-8")
    (wiki_dir / "INDEX.MD").write_text("excluded", encoding="utf-8")
    (wiki_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    nested = wiki_dir / "nested"
    nested.mkdir()
    (nested / "Source_Nested.MD").write_text("nested", encoding="utf-8")

    discovered = {path.name for path in iter_markdown_files(wiki_dir)}

    assert discovered == {"Source_Upper.MD", "Concept_Target.md", "INDEX.MD"}
    assert governance_store._count_wiki_pages() == 2
    assert _wiki_keys() == {"Source_Upper", "Concept_Target"}
    assert {path.name for path in _wiki_files()} == {
        "Source_Upper.MD",
        "Concept_Target.md",
    }

    backlinks = build_wiki_backlink_index(wiki_dir)
    assert backlinks["concept_target"][0][1] == "Source_Upper"
