"""The broken-link stub creator must not fork an entity that already exists.

The existence check used to be prefix-exact, so a link to ``[[Epic Systems]]`` did
not match the existing ``Vendor_Epic-Systems`` page.  The creator then invented
``Concept_Epic-Systems`` -- an auto-stub with ``sources: []``, prefix-variant
aliases and ``categories: [Uncategorized]`` -- and the graph carried the same
entity twice.  Four such pairs were live, and each one also produced a pending
merge item and a set of dangling links once the duplicate was noticed.
"""

from vector_lake.tool_query import _covering_page, _node_core
from vector_lake.wiki_utils import normalize_entity_name


def _index(existing):
    return (
        set(existing),
        {normalize_entity_name(name) for name in existing},
        {_node_core(name): name for name in existing},
    )


def test_a_correctly_typed_page_covers_a_bare_link():
    files, normalized, cores = _index(["Vendor_Epic-Systems", "Person_刘宁"])

    assert _covering_page("Epic-Systems", files, normalized, cores) == "Vendor_Epic-Systems"
    assert _covering_page("Concept_Epic-Systems", files, normalized, cores) == "Vendor_Epic-Systems"
    assert _covering_page("刘宁", files, normalized, cores) == "Person_刘宁"
    assert _covering_page("Concept_刘宁", files, normalized, cores) == "Person_刘宁"


def test_an_exact_page_still_covers_itself():
    files, normalized, cores = _index(["Concept_Known"])

    assert _covering_page("Concept_Known", files, normalized, cores) == "Concept_Known"


def test_a_genuinely_absent_name_is_not_covered():
    files, normalized, cores = _index(["Vendor_Epic-Systems"])

    assert _covering_page("Concept_Something-Else", files, normalized, cores) is None
    assert _covering_page("", files, normalized, cores) is None


def test_the_core_name_strips_any_type_prefix():
    assert _node_core("Vendor_Epic-Systems") == "Epic-Systems"
    assert _node_core("Concept_Epic-Systems") == "Epic-Systems"
    assert _node_core("Epic-Systems") == "Epic-Systems"
    assert _node_core("Event_广东省加快推进人工智能") == "广东省加快推进人工智能"
