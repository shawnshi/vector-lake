"""The broken-link stub creator must not fork an entity that already exists.

The existence check used to be prefix-exact, so a link to ``[[Epic Systems]]`` did
not match the existing ``Vendor_Epic-Systems`` page.  The creator then invented
``Concept_Epic-Systems`` -- an auto-stub with ``sources: []``, prefix-variant
aliases and ``categories: [Uncategorized]`` -- and the graph carried the same
entity twice.  Four such pairs were live, and each one also produced a pending
merge item and a set of dangling links once the duplicate was noticed.
"""

from vector_lake.node_vocabulary import strip_prefix
from vector_lake.stub_creator import covering_page
from vector_lake.wiki_utils import normalize_entity_name


def _index(existing):
    return (
        set(existing),
        {normalize_entity_name(name) for name in existing},
        {strip_prefix(name): name for name in existing},
    )


def test_a_correctly_typed_page_covers_a_bare_link():
    index = _index(["Vendor_Epic-Systems", "Person_刘宁"])

    assert covering_page("Epic-Systems", index) == "Vendor_Epic-Systems"
    assert covering_page("Concept_Epic-Systems", index) == "Vendor_Epic-Systems"
    assert covering_page("刘宁", index) == "Person_刘宁"
    assert covering_page("Concept_刘宁", index) == "Person_刘宁"


def test_an_exact_page_still_covers_itself():
    index = _index(["Concept_Known"])

    assert covering_page("Concept_Known", index) == "Concept_Known"


def test_a_genuinely_absent_name_is_not_covered():
    index = _index(["Vendor_Epic-Systems"])

    assert covering_page("Concept_Something-Else", index) is None
    assert covering_page("", index) is None


def test_the_core_name_strips_any_type_prefix():
    assert strip_prefix("Vendor_Epic-Systems") == "Epic-Systems"
    assert strip_prefix("Concept_Epic-Systems") == "Epic-Systems"
    assert strip_prefix("Epic-Systems") == "Epic-Systems"
    assert strip_prefix("Event_广东省加快推进人工智能") == "广东省加快推进人工智能"
