"""The wiki page-key scan that the canonical write gate reads.

Regression guard for a real availability defect: the key set used to be memoised
on the containing directory's ``(st_mtime_ns, st_size)``.  That is not sound on
NTFS — on this host, creating a file in a directory left the stamp byte-identical,
and 300 rapid creates produced only 42 distinct stamps — so the gate could read a
stale key set and silently drop ``projection_drift`` for a page that had just been
written.
"""

from vector_lake.wiki_utils import wiki_page_keys


def test_a_new_page_is_visible_without_any_sleep(tmp_path):
    (tmp_path / "Concept_A.md").write_text("a", encoding="utf-8")
    assert wiki_page_keys(tmp_path) == {"Concept_A"}

    # No sleep and no directory-stamp change is required for this to be correct.
    (tmp_path / "Concept_B.md").write_text("b", encoding="utf-8")
    assert wiki_page_keys(tmp_path) == {"Concept_A", "Concept_B"}

    (tmp_path / "Concept_A.md").unlink()
    assert wiki_page_keys(tmp_path) == {"Concept_B"}


def test_repeated_reads_are_never_served_from_a_stale_cache(tmp_path):
    for index in range(5):
        (tmp_path / f"Concept_{index}.md").write_text("x", encoding="utf-8")
        assert f"Concept_{index}" in wiki_page_keys(tmp_path)


def test_system_pages_and_non_markdown_entries_are_excluded(tmp_path):
    for name in (
        "Concept_A.md",
        "index.md",
        "log.md",
        "overview.md",
        "orphan_pages.md",
        "wiki_link_stats.md",
        "Synthesis_log.md",
        "System_Meta.md",
        "notes.txt",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Concept_Nested.md").write_text("x", encoding="utf-8")

    assert wiki_page_keys(tmp_path) == {"Concept_A"}


def test_dotted_stems_keep_their_full_key_and_a_custom_exclusion_applies(tmp_path):
    (tmp_path / "Concept_A.B.md").write_text("x", encoding="utf-8")
    (tmp_path / "Concept_B.md").write_text("x", encoding="utf-8")

    assert wiki_page_keys(tmp_path) == {"Concept_A.B", "Concept_B"}
    assert wiki_page_keys(tmp_path, {"Concept_A.B.md"}) == {"Concept_B"}


def test_a_missing_directory_is_an_empty_set(tmp_path):
    assert wiki_page_keys(tmp_path / "nope") == set()


def test_the_default_directory_is_the_active_memory_root(isolated_memory, monkeypatch):
    wiki_dir = isolated_memory / "wiki"
    (wiki_dir / "Concept_Default.md").write_text("x", encoding="utf-8")

    assert wiki_page_keys() == {"Concept_Default"}
