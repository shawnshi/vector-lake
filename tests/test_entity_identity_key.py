"""Identity is case-insensitive; spelling is not.

The defect this pins: `normalize_entity_name` is a *naming* function -- it decides what a page is
called -- so it preserves case, and using it for comparisons meant `[[Concept_WASM]]` did not
resolve against a page named `Concept_wasm`.  Two consequences, in order of severity: on Windows,
where the filesystem is case-insensitive, the "second" page is the *same file* (silent loss rather
than a visible duplicate); and on any platform `stub_creator` writes a second page beside the first.

4 269 of the 7 968 live page names carry an acronym, so case-preserving naming has to stay.  The fix
therefore separates the two questions: :func:`entity_identity_key` is what comparisons go through,
and :func:`normalize_entity_name` still decides filenames.  Measured effect on the live corpus:
one link (`[[Wasm]]`) newly resolves, zero links regress, and the number of ambiguous core names is
unchanged at 36.
"""

from __future__ import annotations

import pytest

from vector_lake import link_resolution, stub_creator
from vector_lake.node_vocabulary import NODE_PREFIXES, strip_prefix
from vector_lake.wiki_utils import entity_identity_key, normalize_entity_name


def test_the_identity_key_folds_case_and_the_naming_function_does_not():
    """The two functions answer different questions and must not be swapped for each other."""
    for spelled, other in [
        ("Concept_WASM", "Concept_wasm"),
        ("WASM", "wasm"),
        ("Concept_AI-Native", "Concept_ai-native"),
        ("Vendor_EpicOps", "Vendor_epicops"),
    ]:
        assert entity_identity_key(spelled) == entity_identity_key(other)
        assert normalize_entity_name(spelled) != normalize_entity_name(other)

    # Naming keeps the author's case, so no page is renamed by this change.
    assert normalize_entity_name("Concept_WASM") == "Concept_WASM"
    assert normalize_entity_name("Vendor_EpicSystems") == "Vendor_EpicSystems"


def test_the_identity_key_still_normalises_separators():
    """Case folding must not replace the underscore/hyphen rule it composes with."""
    assert entity_identity_key("Concept_Local_GPU_Compute") == entity_identity_key("Concept_local-gpu-compute")
    assert entity_identity_key("stqm_tension_edges") == entity_identity_key("stqm-tension-edges")


def test_the_identity_key_leaves_non_ascii_alone():
    assert entity_identity_key("Concept_医院") == "concept_医院"
    assert entity_identity_key("Concept_医院") != entity_identity_key("Concept_医学院")
    # casefold rather than lower: it folds the German sharp s, which lower() does not.
    assert entity_identity_key("Concept_Straße") == entity_identity_key("Concept_STRASSE")


def test_a_case_differing_link_resolves_to_the_existing_page():
    """The reported case: `[[Wasm]]` (or `[[Concept_WASM]]`) must find `Concept_WASM.md`."""
    core_pages, unique_cores = link_resolution.core_name_maps(["Concept_WASM", "Concept_其他"])

    assert link_resolution.resolve_link_target("Wasm", {}, unique_cores) == "Concept_WASM"
    assert link_resolution.resolve_link_target("Concept_wasm", {}, unique_cores) == "Concept_WASM"
    # A *lowercase prefix* is not stripped: prefix stripping stays case-sensitive, which the
    # measurement below (zero such link targets in the corpus) says is safe to leave alone.
    assert link_resolution.resolve_link_target("concept_WASM", {}, unique_cores) is None


def test_a_case_differing_target_does_not_earn_a_second_page(tmp_path):
    """The duplicate-writing path: a covered target must not justify a stub."""
    for name in ("Concept_WASM.md", "Concept_Other.md"):
        (tmp_path / name).write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")

    index = stub_creator.existence_index(str(tmp_path))

    # The covering *page* is what the caller needs: the stub is not created at all.
    assert stub_creator.covering_page("Wasm", index) == "Concept_WASM"
    assert stub_creator.covering_page("Concept_wasm", index) == "Concept_WASM"
    assert stub_creator.covering_page("Concept_WASM", index) == "Concept_WASM"


def test_two_pages_differing_only_in_case_stay_ambiguous(tmp_path):
    """Folding must not silently pick one of two pages that differ only by case."""
    for name in ("Concept_WASM.md", "Product_wasm.md"):
        (tmp_path / name).write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")

    core_pages, unique_cores = link_resolution.core_name_maps(["Concept_WASM", "Product_wasm"])

    assert len(core_pages["wasm"]) == 2
    assert "wasm" not in unique_cores, "an ambiguous name became resolvable"
    assert link_resolution.resolve_link_target("Wasm", {}, unique_cores) is None


def test_strip_prefix_is_left_case_sensitive_by_measurement():
    """No link target in the live corpus uses a lowercase prefix, so this was not changed.

    Keeping it as-is is the conservative choice: making prefix stripping case-insensitive would
    broaden what counts as a typed node, and the measurement says nothing needs it.
    """
    assert strip_prefix("Vendor_Epic-Systems") == "Epic-Systems"
    assert strip_prefix("concept_wasm") == "concept_wasm"  # lowercase prefix is not a node type
    assert all(prefix[0].isupper() for prefix in NODE_PREFIXES if prefix[0].isalpha())


@pytest.mark.parametrize("function_name", ["normalize_entity_name", "entity_identity_key"])
def test_both_helpers_are_importable_from_the_one_owner(function_name):
    """One owner for the vocabulary: importing from anywhere else is how these drifted before."""
    import vector_lake.wiki_utils as wiki_utils

    assert callable(getattr(wiki_utils, function_name))


def test_the_identity_key_folds_separator_punctuation_and_the_naming_function_does_not():
    """``3.5`` and ``3-5`` are one name for comparison and two spellings for naming.

    The reported case: the page is ``Product_Gemini-3-5-Flash`` while prose and links write
    ``Gemini 3.5 Flash``, so the link was reported broken.  Folding happens on *separators* only --
    a letter or digit is never touched -- so no two identifiers can be merged by it, and the naming
    function still keeps the dot because ``Source_2604.24658`` is an arXiv identifier.
    """
    # Compared the way resolution compares them: the prefix is stripped by the caller, so the key
    # function never sees ``Product_``/``Concept_`` on one side only.
    def key(name: str) -> str:
        return entity_identity_key(strip_prefix(name))

    for spelled, other in [
        ("Product_Gemini-3-5-Flash", "Gemini 3.5 Flash"),
        ("Concept_DRG-3.0", "DRG-3-0"),
        ("Concept_v1.2", "v1-2"),
        ("Source_2604.24658v3", "2604-24658v3"),
        ("Concept_A、B", "Concept_A-B"),
        ("Concept_问答？", "Concept_问答"),
    ]:
        assert key(spelled) == key(other)

    # Separators never swallow a letter or a digit: these are different names.
    assert key("Concept_AI") != key("Concept_A-I")
    assert key("Concept_v1.2") != key("Concept_v1.3")

    # Naming keeps the author's punctuation, so no page is renamed by this change.
    assert normalize_entity_name("Concept_DRG-3.0") == "Concept_DRG-3.0"
    assert normalize_entity_name("Source_2604.24658v3") == "Source_2604.24658v3"


def test_an_alias_reaches_the_core_table_but_never_shadows_a_page_name():
    """A link may reach a page by any declared name, including one whose core fallback is an alias.

    Measured on the live corpus: two links newly resolve and none regress.  The precedence matters
    and was measured the hard way -- folding every declared name in unconditionally left 11 links
    unresolved that had resolved before, because one page's alias contested another page's *own*
    name (contested keys went 36 -> 185).
    """
    nodes = ["Concept_甲", "Concept_乙", "Institution_中国医院协会信息专业委员会"]
    declared = {"Institution_CHIMA": ["Institution_中国医院协会信息专业委员会"]}
    _core_pages, unique_cores = link_resolution.core_name_maps(nodes, declared)
    assert link_resolution.resolve_link_target("Institution_CHIMA", {}, unique_cores) == (
        "Institution_中国医院协会信息专业委员会"
    )

    # An alias may not contest a name a page owns.
    declared = {"乙": ["Concept_甲"]}
    core_pages, unique_cores = link_resolution.core_name_maps(nodes, declared)
    assert unique_cores[entity_identity_key("乙")] == "Concept_乙"
    assert link_resolution.resolve_link_target("Concept_乙", {}, unique_cores) == "Concept_乙"


def test_a_name_two_pages_declare_stays_contested_in_the_core_table():
    """Folding declarations in must not resolve an ambiguous name -- only widen what can resolve."""
    nodes = ["Concept_源", "Product_甲", "Vendor_乙"]
    declared = {"Shared": ["Product_甲", "Vendor_乙"]}
    core_pages, unique_cores = link_resolution.core_name_maps(nodes, declared)
    assert len(core_pages[entity_identity_key("Shared")]) == 2
    assert entity_identity_key("Shared") not in unique_cores
    assert link_resolution.resolve_link_target("Shared", {}, unique_cores) is None
