"""One vocabulary for node types, and the ``System_`` bugs its copies caused.

Four hand-written representations of the same list had drifted: the prefix tuple in
``wiki_utils`` (11 prefixes), the one in ``tool_query`` (10 -- no ``System_``, while
the comment above it claimed to mirror ``wiki_utils``), a second inline copy of the
same 10 prefixes later in ``tool_query``, and a regex alternation in
``wiki_utils.validate_wiki_filename`` (also 10).  ``schema_validator`` held the same
vocabulary a fifth time as a set of bare types.

The ``System_`` omission did reach behaviour.  ``_node_core`` stripped every prefix
except ``System_``, and the stub creator labelled a ``System_*`` target ``concept``
-- a type ``validate_schema`` refuses for a ``System_*.md`` filename, so those
targets silently produced no stub.  Once the label is corrected the write succeeds,
which is why the stub creator now refuses generated-artifact types outright: the page
would satisfy the linter while ``indexer`` skips it, hiding the gap.
"""

import ast
import pathlib
import re

from vector_lake import node_vocabulary, schema_validator, stub_creator, tool_query, wiki_utils
from vector_lake.node_vocabulary import (
    GENERATED_NODE_TYPES,
    NODE_PREFIXES,
    NODE_TYPES,
    strip_prefix,
    type_for_node_id,
    type_for_prefix,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_representations_are_the_same_object_not_equal_copies():
    """Identity, not equality: a copy that still matched would pass equality."""
    assert wiki_utils.VALID_PREFIXES is node_vocabulary.NODE_PREFIXES
    assert schema_validator.VALID_TYPES is node_vocabulary.NODE_TYPE_SET


def test_every_prefix_maps_back_to_exactly_its_type():
    assert len(NODE_TYPES) == len(NODE_PREFIXES) == len(set(NODE_TYPES))
    for node_type, prefix in zip(NODE_TYPES, NODE_PREFIXES):
        assert node_vocabulary.prefix_for(node_type) == prefix
        assert type_for_prefix(prefix) == node_type
        assert type_for_node_id(f"{prefix}Something") == node_type

    assert type_for_prefix("Nonsense_") is None
    assert type_for_prefix("Vendor") is None  # no trailing underscore
    assert type_for_prefix("") is None
    assert type_for_node_id("Untyped_Thing") is None
    assert strip_prefix("Untyped_Thing") == "Untyped_Thing"


def test_a_system_node_is_stripped_like_every_other_prefix():
    """The regression: ``System_`` was absent from the copy the stub creator used."""
    for prefix in NODE_PREFIXES:
        assert strip_prefix(f"{prefix}Epic-Systems") == "Epic-Systems"

    assert strip_prefix("Epic-Systems") == "Epic-Systems"
    # ``System_Community_AI`` must not be treated as a prefix-less name.  Note the
    # remainder keeps its underscore: ``strip_prefix`` strips a prefix, it does not
    # normalise, which is why a hyphenated link target does not match this file.
    assert strip_prefix("System_Community_AI") == "Community_AI"


def test_a_stub_for_a_typed_node_declares_that_type():
    for node_type, prefix in zip(NODE_TYPES, NODE_PREFIXES):
        assert stub_creator.stub_type(f"{prefix}Something") == node_type

    assert stub_creator.stub_type("Vendor_New-Thing") == "vendor"
    assert stub_creator.stub_type("Epic-Systems") == "concept"


def test_generated_artifacts_are_marked_and_excluded():
    """A page ``indexer`` skips cannot resolve a link, so a stub must not be written.

    ``System_`` is the generated-artifact namespace: the live wiki holds 798 such
    pages, all produced by the clustering daemon.  Writing a ``System_*`` stub would
    satisfy the linter while staying invisible to the graph.
    """
    assert type_for_node_id("System_Roadmap") in GENERATED_NODE_TYPES
    assert type_for_node_id("Vendor_Roadmap") not in GENERATED_NODE_TYPES
    assert type_for_node_id("Bare-Roadmap") not in GENERATED_NODE_TYPES
    assert GENERATED_NODE_TYPES <= schema_validator.VALID_TYPES


def test_a_link_to_a_generated_artifact_gets_no_page(isolated_memory):
    """Backstop for the rule above, driven through the real function.

    Its strength is limited and that is deliberate: stub creation also needs a
    purpose contract and a valid filename, so ``made == 0`` alone would not prove
    the rule fired.  The rule itself is pinned by the test above; this one checks
    that no ``System_*`` file appears next to a page linking to one.
    """
    from vector_lake import db_store
    from vector_lake.wiki_utils import get_wiki_dir

    db_store.init_db()
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Concept_Source-Page.md").write_text(
        "---\nid: c_sp\ntitle: SP\ntype: concept\ndomain: General\nstatus: Active\n"
        "epistemic-status: seed\ncategories: [Uncategorized]\nupdated: 2026-09-17\n"
        "sources: []\nstrategic_scope: core\nevidence_tier: derived\n---\n\n"
        "Body.\n\n- [[System_Roadmap]]\n",
        encoding="utf-8",
    )

    tool_query._generate_stubs_for_broken_links(str(wiki), {"Concept_Source-Page.md"})

    assert sorted(p.name for p in wiki.glob("System_*.md")) == []


def test_no_module_redeclares_the_prefix_list():
    """A re-declared list carries all 11 literals; a special case carries one or two.

    ``tool_ingest`` and ``tool_lint`` legitimately mention two prefixes each, so the
    guard is a threshold rather than a blanket ban.

    What it does NOT catch, stated plainly: single-quoted literals, f-strings, a list
    assembled from a variable, an alternation built by hand, and anything outside
    ``vector_lake/*.py``.  ``node_vocabulary`` itself is not exempt by name -- it
    stays under the threshold even though its docstrings quote a prefix as an example.
    """
    literals = {f'"{prefix}"' for prefix in NODE_PREFIXES}
    offenders = {}
    for path in sorted((ROOT / "vector_lake").glob("*.py")):
        found = {
            literal for literal in literals if literal in path.read_text(encoding="utf-8")
        }
        if len(found) >= 3:
            offenders[str(path)] = sorted(found)

    assert offenders == {}, (
        "these modules spell the prefix vocabulary out again instead of importing "
        f"node_vocabulary: {offenders}"
    )


def test_the_guard_would_notice_a_redeclaration():
    """A guard that cannot fail is not a guard: exercise its threshold on real text."""
    literals = [f'"{prefix}"' for prefix in NODE_PREFIXES]
    redeclared = "\n".join(literals)

    found = {literal for literal in literals if literal in redeclared}

    assert len(found) == len(NODE_PREFIXES)
    assert len(found) >= 3


def test_the_strict_filename_pattern_covers_the_whole_vocabulary():
    """That alternation was the fourth copy, and it was missing ``System``.

    A filename for a type it omits is accepted by ``startswith(VALID_PREFIXES)`` and
    then rejected by the stricter pattern, so growing the vocabulary alone would have
    split the two checks.
    """
    pattern = re.compile(
        rf"^(?:{node_vocabulary.NODE_TYPE_ALTERNATION})_"
        r"[a-zA-Z0-9\u4e00-\u9fa5]+(-[a-zA-Z0-9\u4e00-\u9fa5]+)*\.md$"
    )

    for prefix in NODE_PREFIXES:
        assert pattern.match(f"{prefix}Some-Name.md"), prefix

    assert not pattern.match("Nonsense_Some-Name.md")


def test_the_vocabulary_module_imports_nothing():
    """It is imported by every layer, so it must not import one back.

    Parsed rather than pattern-matched: the first version of this test only looked
    for ``vector_lake`` imports, so ``import os`` or an ``importlib`` call would have
    satisfied the module docstring's "no imports" claim while breaking it.
    """
    source = pathlib.Path(node_vocabulary.__file__).read_text(encoding="utf-8")

    found = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]

    assert found == [], [ast.dump(node) for node in found]


def test_a_generated_index_is_recognised_without_a_marker():
    """The daemon's two H2 sections are identity too, and seven pages had only that.

    They carried no ``community_id``, no ``level`` and no ``System_Community_`` name -- and the
    earlier reading of that gap, "knowledge pages misfiled under System_", was wrong: their body
    opens ``# L0 Comm: ...`` and carries the generated-index note.  Identity by shape is what
    stops such a page from being classified, or merged, as if it were knowledge.
    """
    from vector_lake.node_vocabulary import is_generated_artifact

    artifact_body = (
        "# L0 Comm: A cluster\n\n"
        "> [!NOTE]\n> 这是一个系统自动生成的社区索引文件\n\n"
        "## 核心节点 (Hubs)\n\n## 社区成员 (Members)\n"
    )

    assert is_generated_artifact({}, "System_Topic.md", artifact_body) is True
    assert is_generated_artifact({}, "Concept_Topic.md", artifact_body) is True
    assert is_generated_artifact({}, "Concept_Topic.md", "## 1. 编译事实\n\n- x\n") is False
    assert is_generated_artifact({"community_id": 7}, "Concept_Topic.md") is True
