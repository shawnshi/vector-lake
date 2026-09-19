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
